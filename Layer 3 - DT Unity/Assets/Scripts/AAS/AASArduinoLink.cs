using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Text;
using System.Threading;
using UnityEngine;
using Newtonsoft.Json.Linq;

/// <summary>
/// Singleton that manages shared TCP connections to Arduino hardware endpoints.
/// Supports multiple connections (one per unique IP:port combination).
/// All AASSensorController instances subscribe here instead of opening their own connections.
/// 
/// Architecture:
///   Arduino A (192.168.10.1:8888) <-> ConnectionState thread A
///   Arduino B (192.168.10.2:9000) <-> ConnectionState thread B
///          \\                    /
///           AASArduinoLink.Instance
///                    |
///        port callbacks -> all controllers
/// </summary>
public class AASArduinoLink : MonoBehaviour
{
    // -----------------------------------------------------------------------
    // Singleton
    // -----------------------------------------------------------------------
    public static AASArduinoLink Instance { get; private set; }

    void Awake()
    {
        if (Instance != null && Instance != this) { Destroy(gameObject); return; }
        Instance = this;
        DontDestroyOnLoad(gameObject);
    }

    // -----------------------------------------------------------------------
    // Per-endpoint connection state
    // -----------------------------------------------------------------------
    private class ConnectionState
    {
        public string ip;
        public int    port;
        public Thread thread;
        public volatile bool running;
        public volatile bool isConnected;
        public System.Net.Sockets.TcpClient client;
    }

    private readonly object _connLock = new object();
    private readonly Dictionary<string, ConnectionState> _connections = new();
    private bool _allowConnections = false;

    public int RegisteredEndpointCount
    {
        get
        {
            lock (_connLock) return _connections.Count;
        }
    }

    public bool HasActiveConnections()
    {
        lock (_connLock)
        {
            if (_connections.Count == 0) return false;
            foreach (var cs in _connections.Values)
            {
                if (cs.isConnected) return true;
            }
            return false;
        }
    }

    public bool AreAllConnected()
    {
        lock (_connLock)
        {
            if (_connections.Count == 0) return false;
            foreach (var cs in _connections.Values)
            {
                if (!cs.isConnected) return false;
            }
            return true;
        }
    }

    private static string Key(string ip, int port) => $"{ip}:{port}";

    // -----------------------------------------------------------------------
    // Subscriber registry  portId -> list of callbacks
    // -----------------------------------------------------------------------
    private readonly object _regLock = new object();
    private readonly Dictionary<string, List<Action<bool>>> _subscribers = new();

    public void Subscribe(string portId, Action<bool> callback)
    {
        lock (_regLock)
        {
            if (!_subscribers.TryGetValue(portId, out var list))
            {
                list = new List<Action<bool>>();
                _subscribers[portId] = list;
            }
            if (!list.Contains(callback))
                list.Add(callback);
        }
    }

    public void Unsubscribe(string portId, Action<bool> callback)
    {
        lock (_regLock)
        {
            if (_subscribers.TryGetValue(portId, out var list))
                list.Remove(callback);
        }
    }

    // -----------------------------------------------------------------------
    // Main-thread event queue (all connections enqueue here)
    // -----------------------------------------------------------------------
    private readonly ConcurrentQueue<(string portId, bool value)> _queue = new();

    // -----------------------------------------------------------------------
    // Connection management
    // -----------------------------------------------------------------------

    /// <summary>
    /// Start a TCP connection to ip:port if one isn't already running.
    /// Idempotent: calling multiple times with the same endpoint is safe.
    /// </summary>
    public void StartConnection(string ip, int port)
    {
        string key = Key(ip, port);
        lock (_connLock)
        {
            if (_connections.TryGetValue(key, out var existing))
            {
                if (_allowConnections && !existing.running)
                {
                    StartThread(existing);
                }
                return;
            }

            var cs = new ConnectionState { ip = ip, port = port, running = false };
            _connections[key] = cs;
            Debug.Log($"[ArduinoLink] Registered connection endpoint for {key}");
            
            if (_allowConnections)
            {
                StartThread(cs);
            }
        }
    }

    private void StartThread(ConnectionState cs)
    {
        string key = Key(cs.ip, cs.port);
        cs.running = true;
        cs.thread = new Thread(() => ReceiveLoop(cs))
        {
            IsBackground = true,
            Name         = $"ArduinoLink_{key}"
        };
        cs.thread.Start();
        Debug.Log($"[ArduinoLink] Thread started for TCP connection to {key}");
    }

    public void ConnectAll()
    {
        lock (_connLock)
        {
            Debug.Log($"[ArduinoLink] ConnectAll() called. Allowing connections and starting {_connections.Count} pending connections.");
            _allowConnections = true;
            foreach (var cs in _connections.Values)
            {
                if (!cs.running)
                {
                    StartThread(cs);
                }
            }
        }
    }

    /// <summary>Stop the connection to a specific ip:port.</summary>
    public void StopConnection(string ip, int port)
    {
        string key = Key(ip, port);
        ConnectionState cs;
        lock (_connLock)
        {
            if (!_connections.TryGetValue(key, out cs)) return;
            _connections.Remove(key);
        }
        cs.running = false;
        try { cs.client?.Close(); } catch { }
        cs.thread?.Join(400);
        Debug.Log($"[ArduinoLink] Stopped connection to {key}");
    }

    public void StopAllConnections()
    {
        List<ConnectionState> all;
        lock (_connLock)
        {
            all = new List<ConnectionState>(_connections.Values);
            _connections.Clear();
            _allowConnections = false;
        }
        foreach (var cs in all)
        {
            cs.running = false;
            try { cs.client?.Close(); } catch { }
            cs.thread?.Join(400);
        }
        while (_queue.TryDequeue(out _)) { }
        Debug.Log("[ArduinoLink] All connections stopped.");
    }

    // -----------------------------------------------------------------------
    // Background TCP receive loop (one per ConnectionState)
    // -----------------------------------------------------------------------
    private void ReceiveLoop(ConnectionState cs)
    {
        while (cs.running)
        {
            try
            {
                Debug.Log($"[ArduinoLink-LOG] Attempting TCP connection to {cs.ip}:{cs.port}");
                cs.client = new System.Net.Sockets.TcpClient();
                cs.client.Connect(cs.ip, cs.port);
                cs.client.ReceiveTimeout = 0;

                using var stream = cs.client.GetStream();
                cs.isConnected = true;
                Debug.Log($"[ArduinoLink-LOG] Connected successfully to {cs.ip}:{cs.port}");

                var buf        = new StringBuilder();
                int braceDepth = 0;

                while (cs.running && cs.client.Connected)
                {
                    int b = stream.ReadByte();
                    if (b < 0) break;

                    char c = (char)b;
                    if (c == '{') braceDepth++;
                    else if (c == '}') braceDepth--;
                    buf.Append(c);

                    if (braceDepth == 0 && buf.Length > 2)
                    {
                        ParseAndEnqueue(buf.ToString());
                        buf.Clear();
                    }
                }
            }
            catch (Exception ex)
            {
                if (cs.running)
                {
                    Debug.LogWarning($"[ArduinoLink-LOG] TCP error ({cs.ip}:{cs.port}): {ex.Message} - retrying in 3s");
                    Thread.Sleep(3000);
                }
            }
            finally
            {
                cs.isConnected = false;
                try { cs.client?.Close(); } catch { }
                cs.client = null;
            }
        }
    }

    private void ParseAndEnqueue(string json)
    {
        try
        {
            Debug.Log($"[ArduinoLink-LOG] Received JSON: '{json}'");
            var obj = JObject.Parse(json);
            
            // Handle both "Port" and "port"
            string id = (obj["Port"] ?? obj["port"])?.ToString();
            
            bool val = false;
            var valToken = obj["Value"] ?? obj["value"];
            if (valToken != null)
            {
                if (valToken.Type == JTokenType.Boolean)
                {
                    val = (bool)valToken;
                }
                else if (valToken.Type == JTokenType.Integer)
                {
                    val = (int)valToken == 1;
                }
                else
                {
                    string strVal = valToken.ToString().ToLower();
                    val = (strVal == "1" || strVal == "true");
                }
                
                if (id != null)
                {
                    Debug.Log($"[ArduinoLink-LOG] Enqueuing event: Port='{id}', Value={val}");
                    _queue.Enqueue((id, val));
                }
                else
                {
                    Debug.LogWarning($"[ArduinoLink-LOG] Port ID is null in JSON: '{json}'");
                }
            }
            else
            {
                Debug.LogWarning($"[ArduinoLink-LOG] Value token is null in JSON: '{json}'");
            }
        }
        catch (Exception ex)
        {
            Debug.LogError($"[ArduinoLink-LOG] Exception parsing JSON '{json}': {ex.Message}\n{ex.StackTrace}");
        }
    }

    // -----------------------------------------------------------------------
    // Main-thread dispatch
    // -----------------------------------------------------------------------
    void Update()
    {
        while (_queue.TryDequeue(out var evt))
        {
            List<Action<bool>> callbacks;
            lock (_regLock)
            {
                _subscribers.TryGetValue(evt.portId, out callbacks);
            }
            if (callbacks != null)
                foreach (var cb in callbacks)
                    cb?.Invoke(evt.value);
        }
    }

    void OnDestroy()
    {
        if (Instance == this)
        {
            StopAllConnections();
            Instance = null;
        }
    }
}
