using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Networking;
using Newtonsoft.Json.Linq;
using System.Linq;

public class AASSensorController : MonoBehaviour
{
    private string pinsUrl;
    private string behaviorUrl;

    [Serializable]
    public class SensorBinding
    {
        public string pinId;
        public string componentName;
        public string type;
        public string targetFilter;
        public bool lastState = false;
        public bool realState = false; // Real-world state from Arduino TCP Sockets
        public int overlapCount = 0; // For TriggerZone
        public bool activeLow = false; // Added for Active Low support
        public bool visible = true;
        
        public string snapTarget;
        public Vector3 snapPosition;
        public bool hasSnap;
        public float lastSnapTime = -99f; // Cooldown timer to prevent infinite re-snaps

        // --- Move-On-State ---
        // When the sensor reaches moveOnState, the component named moveTargetName
        // is smoothly moved to moveTargetLocalPos / moveTargetLocalRot (local space).
        // Any non-kinematic Rigidbody physically overlapping the target is carried along.
         public string moveTargetName;
        public bool? moveOnState;          // null = feature disabled
        public string moveMode = "Both";   // "Both", "Position", "Rotation"
        public Vector3 moveTargetLocalPos;
        public Vector3 moveTargetLocalRot; // Euler angles, local space
        public float moveDuration;         // seconds; 0 = instant snap
        public bool hasMoveConfig;
        [System.NonSerialized] public Coroutine activeMoveCoroutine;

        // --- Digital Twin: physical port on the real hardware ---
        // The hardware pushes JSON events: {"Port":"<realPortId>","Value":0|1}
        public bool   realSensorEnabled;
        public string realPortId;  // e.g. "I0_0", "sensor_arrival" – matches hardware Port field
        [System.NonSerialized] public GameObject currentProductAtSensor;
    }

    private Dictionary<string, SensorBinding> componentToSensor = new Dictionary<string, SensorBinding>();
    private Dictionary<string, GameObject>    _componentMap     = new Dictionary<string, GameObject>();
    
    // Globally keeps track of the last product that left any sensor
    private static Rigidbody lastDetectedProduct = null;

    // portId (from hardware) → SensorBinding — built after LoadBindingsRoutine
    private Dictionary<string, SensorBinding> _portIdToBinding  = new Dictionary<string, SensorBinding>();
    private HashSet<string> _syncedPorts = new HashSet<string>();
    private float _dtConnectionTime = 0f;
    private bool _bindingsLoaded = false;
    private bool isStabilized = false;

    // --- Digital Twin: hardware endpoint (read from AAS AssetInterfacesDescription) ---
    private string _kitProtocol    = "tcp";
    private string _kitEndpointIP  = "";
    private int    _kitEndpointPort = 8888;

    // Stores port->callback mappings so we can properly unsubscribe from AASArduinoLink
    private readonly System.Collections.Generic.Dictionary<string, Action<bool>> _portCallbacks = new();
    private bool _dtActive = false;

    public void Init(string serverUrl, string pinsSubmodelId, string behaviorSubmodelId,
                     Dictionary<string, GameObject> componentMap,
                     string kitProtocol = "tcp", string kitEndpointIP = "", int kitEndpointPort = 8888,
                     float delay = 10.0f)
    {
        _kitProtocol     = (kitProtocol ?? "tcp").ToLower();
        _kitEndpointIP   = kitEndpointIP ?? "";
        _kitEndpointPort = kitEndpointPort;
        // Inputs = sensor readings (what enters the PLC/system)
        this.pinsUrl     = $"{serverUrl}/submodels/{Base64Url(pinsSubmodelId)}/submodel-elements/Inputs";
        this.behaviorUrl = $"{serverUrl}/submodels/{Base64Url(behaviorSubmodelId)}/submodel-elements";
        
        isStabilized = true;
        StartCoroutine(LoadBindingsRoutine(componentMap));
    }

    public bool GetSensorState(string pinId, out bool state, bool useReal = false)
    {
        state = false;
        foreach (var kvp in componentToSensor)
        {
            if (kvp.Value.pinId == pinId)
            {
                state = useReal ? kvp.Value.realState : kvp.Value.lastState;
                return true;
            }
        }
        return false;
    }

    public bool HasSnapConfig(string sensorName)
    {
        foreach (var kvp in componentToSensor)
        {
            if (kvp.Value.pinId == sensorName || kvp.Value.componentName == sensorName)
            {
                return kvp.Value.hasSnap;
            }
        }
        return false;
    }

    void OnDestroy()
    {
        OperationModeManager.OnModeChanged -= OnOperationModeChanged;
        StopDTConnection();
    }

    IEnumerator LoadBindingsRoutine(Dictionary<string, GameObject> componentMap)
    {
        _componentMap = componentMap;

        UnityWebRequest req = UnityWebRequest.Get(behaviorUrl + "?level=deep");
        yield return req.SendWebRequest();

        if (req.result == UnityWebRequest.Result.Success)
        {
            JToken root = JToken.Parse(req.downloadHandler.text);
            JArray elements = (root is JObject obj && obj["result"] != null) ? obj["result"] as JArray : root as JArray;

            if (elements != null)
            {
                foreach (var el in elements)
                {
                    string pinId = el["idShort"]?.ToString();
                    JToken values = el["value"];
                    if (values == null) continue;

                    string compName = "";
                    string type = "";
                    string filter = "";
                    bool activeLow = false;
                    bool? visibleParam = null;
                    string snapTarget = null;
                    Vector3 snapPos = Vector3.zero;
                    bool hasSnap = false;

                    // Move-on-state
                    string moveTargetName = null;
                    bool? moveOnState = null;
                    string moveMode = "Both";
                    Vector3 movePos = Vector3.zero;
                    Vector3 moveRot = Vector3.zero;
                    float moveDuration = 0f;
                    // DT real-hardware identity
                    bool   realSensorEnabled = false;
                    string realPortId = "";

                    foreach (var val in values)
                    {
                        string id = val["idShort"]?.ToString();
                        if (id == "PinID") pinId = val["value"]?.ToString();
                        if (id == "Component") compName = val["value"]?.ToString();
                        if (id == "Type") type = val["value"]?.ToString();
                        if (id == "Parameters" && val["value"] != null)
                        {
                            foreach (var p in val["value"])
                            {
                                string pid = p["idShort"]?.ToString();
                                if (pid == "TargetFilter") filter = p["value"]?.ToString();
                                if (pid == "ActiveLow") activeLow = p["value"]?.ToString().ToLower() == "true";
                                if (pid == "Visible") visibleParam = p["value"]?.ToString().ToLower() == "true";
                                if (pid == "SnapTarget") { snapTarget = p["value"]?.ToString(); hasSnap = true; }
                                // Move-on-state params
                                if (pid == "MoveTarget") moveTargetName = p["value"]?.ToString();
                                if (pid == "MoveMode")   moveMode = p["value"]?.ToString() ?? "Both";
                                if (pid == "MoveOnState")
                                {
                                    string mv = p["value"]?.ToString()?.ToLower();
                                    if (mv == "true") moveOnState = true;
                                    else if (mv == "false") moveOnState = false;
                                }
                                // Digital Twin: physical hardware port ID
                                 if (pid == "RealSensorEnabled") realSensorEnabled = p["value"]?.ToString()?.ToLower() == "true";
                                 if (pid == "RealPortID")        realPortId         = p["value"]?.ToString() ?? "";

                                float fv = 0f;
                                if (p["value"] != null) float.TryParse(p["value"].ToString().Replace(',', '.'), System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out fv);
                                if (pid == "SnapPosX") snapPos.x = fv;
                                if (pid == "SnapPosY") snapPos.y = fv;
                                if (pid == "SnapPosZ") snapPos.z = fv;
                                if (pid == "MovePosX") movePos.x = fv;
                                if (pid == "MovePosY") movePos.y = fv;
                                if (pid == "MovePosZ") movePos.z = fv;
                                if (pid == "MoveRotX") moveRot.x = fv;
                                if (pid == "MoveRotY") moveRot.y = fv;
                                if (pid == "MoveRotZ") moveRot.z = fv;
                                if (pid == "MoveDuration") moveDuration = fv;
                            }
                        }
                    }

                    bool visible = visibleParam.HasValue ? visibleParam.Value : (type != "TriggerZone");

                    if (!string.IsNullOrEmpty(compName))
                    {
                        GameObject target = FindTargetComponent(componentMap, compName);

                        if (target != null && (type == "TriggerZone" || type == "ContactSensor"))
                        {
                            var binding = new SensorBinding {
                                pinId = pinId,
                                componentName = compName,
                                type = type,
                                targetFilter = filter,
                                activeLow = activeLow,
                                visible = visible,
                                snapTarget = snapTarget,
                                snapPosition = snapPos,
                                hasSnap = hasSnap,
                                moveTargetName = moveTargetName,
                                moveOnState = moveOnState,
                                moveMode = moveMode,
                                moveTargetLocalPos = movePos,
                                moveTargetLocalRot = moveRot,
                                moveDuration = moveDuration,
                                hasMoveConfig = !string.IsNullOrEmpty(moveTargetName) && moveOnState.HasValue,
                                realSensorEnabled = realSensorEnabled,
                                realPortId = realPortId
                            };
                            componentToSensor[compName] = binding;
                            
                            
                            // Setup Unity Physics Proxy
                            var proxy = target.AddComponent<SensorPhysicsProxy>();
                            proxy.binding = binding;
                            proxy.onStateChange += (state, isOverlapping, sensorObj, productObj) =>
                            {
                                if (!isStabilized) return;
                                
                                // Only push virtual physics states to AAS if NOT in Digital Twin mode.
                                // In DT mode, the real physical PLC updates the AAS.
                                if (OperationModeManager.Current != OperationMode.DigitalTwin)
                                {
                                    StartCoroutine(UpdateAASPin(binding, state, isOverlapping, sensorObj, productObj));
                                }
                                
                                // Trigger move-on-state if configured (only in non-DT modes, since DT handles this via hardware events)
                                if (OperationModeManager.Current != OperationMode.DigitalTwin && binding.hasMoveConfig && state == binding.moveOnState.Value)
                                {
                                    if (binding.activeMoveCoroutine != null) StopCoroutine(binding.activeMoveCoroutine);
                                    binding.activeMoveCoroutine = StartCoroutine(ExecuteMoveRoutine(binding));
                                }
                            };

                             // IMPORTANT: Sensors need a Rigidbody (Kinematic) to guarantee trigger events and prevent physics pushes
                             var rb = target.GetComponent<Rigidbody>();
                             if (rb == null) {
                                 rb = target.AddComponent<Rigidbody>();
                             }
                             rb.isKinematic = true;
                             rb.useGravity = false;
 
                             // Ensure collider is set correctly
                             var colliders = target.GetComponentsInChildren<Collider>();
                             if (colliders.Length == 0) {
                                 var box = target.AddComponent<BoxCollider>();
                                 box.isTrigger = true;
                             }
 
                             foreach (var c in target.GetComponentsInChildren<Collider>())
                             {
                                 c.isTrigger = true;
                             }

                            // Auto-hide or show the sensor mesh based on visibility if explicitly defined in AAS
                            if (visibleParam.HasValue)
                            {
                                foreach (var renderer in target.GetComponentsInChildren<Renderer>())
                                {
                                    renderer.enabled = visibleParam.Value;
                                }
                            }

                            Debug.Log($"[Sensor] 👁️ Configured {pinId} on {compName} (Filter: {filter})");
                        }
                        else if (target == null)
                        {
                            Debug.LogWarning($"[Sensor] ⚠️ Could not find component '{compName}' in the model to attach sensor '{pinId}'.");
                        }
                    }
                }
            }
        }

        // Build portId → binding lookup for DT mode
        _portIdToBinding.Clear();
        foreach (var b in componentToSensor.Values)
            if (b.realSensorEnabled && !string.IsNullOrEmpty(b.realPortId))
                _portIdToBinding[b.realPortId] = b;

        _bindingsLoaded = true;
        OperationModeManager.OnModeChanged += OnOperationModeChanged;
        ApplyOperationMode(OperationModeManager.Current);
    }

    // -----------------------------------------------------------------------
    // Operation mode switching
    // -----------------------------------------------------------------------
    private void OnOperationModeChanged(OperationMode mode) => ApplyOperationMode(mode);

    private void ApplyOperationMode(OperationMode mode)
    {
        if (!_bindingsLoaded) return;
        bool isVC = mode == OperationMode.VirtualCommissioning;

        // Enable / disable physics-based sensors
        foreach (var proxy in GetComponentsInChildren<SensorPhysicsProxy>(true))
        {
            if (isVC || mode == OperationMode.DigitalTwin) proxy.ResetAndEnable();
            else      proxy.enabled = false;
        }

        // Start / stop hardware protocol connection
        StopDTConnection();
        if (!isVC)
        {
            if (string.IsNullOrEmpty(_kitEndpointIP))
                Debug.LogWarning($"[Sensor] ⚠️  DT mode on '{gameObject.name}' but no EndpointIP configured in AAS.");
            else
            {
                StartDTConnection();
            }
        }
        Debug.Log($"[Sensor] 🔄 Mode → {mode} on '{gameObject.name}'");
    }

    // -----------------------------------------------------------------------
    // Digital Twin: connect via the shared AASArduinoLink singleton.
    // The Arduino only accepts ONE TCP client — the singleton holds that connection
    // and distributes port-change events to all registered controllers.
    // -----------------------------------------------------------------------
    private void StartDTConnection()
    {
        if (OperationModeManager.Current == OperationMode.DigitalTwin && !BaSyxManager.IsStartupComplete)
        {
            Debug.Log($"[Sensor] ⏳ DT Connection for '{gameObject.name}' delayed until BaSyxManager startup is complete.");
            StartCoroutine(WaitAndStartDTConnection());
            return;
        }

        string proto = (_kitProtocol ?? "tcp").ToLower().Trim();
        if (proto != "tcp" && proto != "socket" && proto != "sockets" && proto != "none")
        {
            Debug.LogWarning($"[Sensor] ⚠️  Protocol '{_kitProtocol}' not yet implemented. Only 'tcp'/'socket'/'sockets' are supported.");
            return;
        }

        _syncedPorts.Clear();
        _dtConnectionTime = Time.time;
        _dtActive = true;

        // Ensure the singleton exists in the scene
        if (AASArduinoLink.Instance == null)
        {
            var go = new GameObject("AASArduinoLink");
            go.AddComponent<AASArduinoLink>();
        }

        // Tell the singleton to (re)connect if needed — idempotent for same IP:port
        AASArduinoLink.Instance.StartConnection(_kitEndpointIP, _kitEndpointPort);

        // Subscribe each binding's realPortId to receive callbacks on the main thread
        foreach (var kvp in _portIdToBinding)
        {
            var binding = kvp.Value;
            if (string.IsNullOrEmpty(binding.realPortId)) continue;

            var b = binding;
            Action<bool> cb = (value) => OnArduinoPortChanged(b, value);
            _portCallbacks[binding.realPortId] = cb;
            AASArduinoLink.Instance.Subscribe(binding.realPortId, cb);
        }

        Debug.Log($"[Sensor] 🔗 DT subscribed to ArduinoLink ({_kitEndpointIP}:{_kitEndpointPort}) for {_portCallbacks.Count} port(s) on '{gameObject.name}'");
    }

    private IEnumerator WaitAndStartDTConnection()
    {
        while (!BaSyxManager.IsStartupComplete)
        {
            yield return new WaitForSeconds(0.5f);
        }

        if (OperationModeManager.Current == OperationMode.DigitalTwin && _dtActive == false)
        {
            Debug.Log($"[Sensor] 🚀 BaSyxManager startup complete. Activating DT Connection for '{gameObject.name}'.");
            StartDTConnection();
        }
    }

    public bool IsInitialDTSyncComplete()
    {
        if (OperationModeManager.Current != OperationMode.DigitalTwin) return true;
        if (!_dtActive) return false;
        return _syncedPorts.Count >= _portIdToBinding.Count || (Time.time - _dtConnectionTime > 2.0f);
    }

    private void StopDTConnection()
    {
        _dtActive = false;
        if (AASArduinoLink.Instance != null)
        {
            foreach (var kvp in _portCallbacks)
                AASArduinoLink.Instance.Unsubscribe(kvp.Key, kvp.Value);
        }
        _portCallbacks.Clear();
        _syncedPorts.Clear();
    }

    // Called on the main thread by AASArduinoLink when a real sensor port changes.
    private void OnArduinoPortChanged(SensorBinding binding, bool value)
    {
        if (!_dtActive) return;
        bool isFirstSync = _syncedPorts.Add(binding.realPortId);
        if (value != binding.realState || isFirstSync)
        {
            binding.realState = value;
            OnDTSensorStateChanged(binding, value);
        }
    }



    // Called on the main Unity thread when a real sensor changes state in DT mode.
    // Triggers the same visual behaviours as VC mode but does NOT write to AAS
    // (the real hardware or skill server is responsible for AAS state).
    private void OnDTSensorStateChanged(SensorBinding binding, bool state)
    {
        bool sensorActive = binding.activeLow ? !state : state;
        Debug.Log($"[DT-LOG] 📡 OnDTSensorStateChanged called for realPortId='{binding.realPortId}', pinId='{binding.pinId}'. state={state}, activeLow={binding.activeLow} -> sensorActive={sensorActive}");

        // --- MOVE CONFIG LOGIC ---
        if (binding.hasMoveConfig && binding.moveOnState.HasValue && state == binding.moveOnState.Value)
        {
            Debug.Log($"[DT-LOG] 🔀 Starting ExecuteMoveRoutine for targetName='{binding.moveTargetName}'");
            if (binding.activeMoveCoroutine != null) StopCoroutine(binding.activeMoveCoroutine);
            binding.activeMoveCoroutine = StartCoroutine(ExecuteMoveRoutine(binding));
        }

        // --- DT SNAP LOGIC ---
        if (sensorActive && binding.hasSnap && (Time.time - binding.lastSnapTime > 1.5f))
        {
            binding.lastSnapTime = Time.time;
            GameObject sensorObj = FindTargetComponent(_componentMap, binding.componentName);
            StartCoroutine(DTSnapWithDelayRoutine(binding, sensorObj));
        }
    }

    private IEnumerator DTSnapWithDelayRoutine(SensorBinding binding, GameObject sensorObj)
    {
        Debug.Log($"[DT-LOG] DTSnapWithDelayRoutine started. SensorObj={sensorObj?.name}");
        yield return new WaitForSeconds(0.3f);
 
        GameObject resolvedSensorObj = sensorObj;
        if (resolvedSensorObj == null)
        {
            // Try to search for the sensor component in the scene using its binding name
            resolvedSensorObj = FindTargetComponent(_componentMap, binding.componentName);
            Debug.Log($"[DT-LOG] DTSnap resolvedSensorObj (fallback)={resolvedSensorObj?.name}");
        }
 
        Transform sensorTrans = resolvedSensorObj != null ? resolvedSensorObj.transform : transform;
 
        Rigidbody targetRb = null;
 
        // Find the closest dynamic product marked by AAS configuration
        var dynamicProducts = FindObjectsByType<AAS.DynamicProduct>(FindObjectsSortMode.None);
        Debug.Log($"[DT-LOG] DTSnap: found {dynamicProducts.Length} dynamic products in scene.");
        float minDist = float.MaxValue;
 
        foreach (var dp in dynamicProducts)
        {
            var rb = dp.GetComponentInParent<Rigidbody>() ?? dp.GetComponent<Rigidbody>();
            if (rb != null)
            {
                float d = Vector3.Distance(rb.position, sensorTrans.position);
                Debug.Log($"[DT-LOG] DTSnap: checking product '{dp.name}', dist={d:F4}");
                if (d < minDist)
                {
                    minDist = d;
                    targetRb = rb;
                }
            }
        }
 
        if (targetRb == null)
        {
            Debug.LogWarning($"[DT-LOG] ⚠️ Snap: no dynamic product found in scene with 'DynamicProduct' component.");
            yield break;
        }

 
        // Keep track of the currently snapped product on this sensor binding
        binding.currentProductAtSensor = targetRb.gameObject;
        Debug.Log($"[DT-LOG] DTSnap closest product: '{targetRb.name}' at distance {minDist:F4}");
 
        Vector3 snapPos;
        float targetY = (targetRb.position.y < -1.0f) ? (sensorTrans.position.y + 0.05f) : targetRb.position.y;
 
        if (binding.snapPosition.sqrMagnitude < 0.0001f)
        {
            if (resolvedSensorObj != null)
                snapPos = new Vector3(resolvedSensorObj.transform.position.x, targetY, resolvedSensorObj.transform.position.z);
            else
                snapPos = new Vector3(sensorTrans.position.x, targetY, sensorTrans.position.z);
        }
        else
        {
            snapPos = new Vector3(binding.snapPosition.x, binding.snapPosition.y, -binding.snapPosition.z);
        }
 
        float dist = Vector3.Distance(targetRb.position, snapPos);
        float angle = Quaternion.Angle(targetRb.rotation, Quaternion.Euler(0, sensorTrans.eulerAngles.y, 0));
        Debug.Log($"[DT-LOG] DTSnap target pos: {snapPos}, current pos: {targetRb.position}, dist={dist:F4}, angle={angle:F2}");
 
        if (dist > 0.02f || angle > 2f)
        {
            targetRb.position = snapPos;
            targetRb.rotation = Quaternion.Euler(0, sensorTrans.eulerAngles.y, 0);
            
            // Toggle kinematic state to force PhysX to clear all accumulated solver forces/velocities
            bool wasKinematic = targetRb.isKinematic;
            targetRb.isKinematic = true;
            targetRb.isKinematic = wasKinematic;
 
            targetRb.linearVelocity = Vector3.zero;
            targetRb.angularVelocity = Vector3.zero;
            targetRb.ResetCenterOfMass();
            targetRb.ResetInertiaTensor();
            Physics.SyncTransforms();
            
            Debug.Log($"[DT-LOG] ⚡ DT SNAP SUCCESS! Teleported '{targetRb.name}' → {snapPos} via {binding.pinId} (Dist={dist:F4}, Angle={angle:F2}º) - Physics Reset");
        }
        else
        {
            Debug.Log($"[DT-LOG] DTSnap: no teleport needed (already close enough: dist={dist:F4}, angle={angle:F2})");
        }
    }


    private bool IsSensorRelatedToActiveSkill(string pinId)
    {
        var engines = FindObjectsByType<AASRuleEngine>(FindObjectsSortMode.None);
        foreach (var eng in engines)
        {
            if (eng.activeSequences.Count > 0)
            {
                foreach (string activeSeq in eng.activeSequences)
                {
                    var seqToken = eng.GetSequenceToken(activeSeq);
                    if (seqToken != null && TokenReferencesSensor(seqToken, pinId, eng.GetLocalKitName()))
                    {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private bool TokenReferencesSensor(JToken token, string pinId, string localKitName)
    {
        if (token == null) return false;
        if (token is JValue) return false;
        
        if (token is JArray arr)
        {
            foreach (var child in arr)
            {
                if (TokenReferencesSensor(child, pinId, localKitName)) return true;
            }
        }
        
        if (token is JObject obj)
        {
            string sens = obj["sensor"]?.ToString();
            if (sens == pinId)
            {
                string kit = obj["kit"]?.ToString();
                if (string.IsNullOrEmpty(kit) || kit == localKitName || kit == gameObject.name)
                    return true;
            }
            foreach (var pair in obj)
            {
                if (TokenReferencesSensor(pair.Value, pinId, localKitName)) return true;
            }
        }
        return false;
    }

    private GameObject FindTargetComponent(Dictionary<string, GameObject> map, string compName)
    {
        if (string.IsNullOrEmpty(compName)) return null;

        // 1. First priority: Exact case-sensitive match in map
        if (map.TryGetValue(compName, out GameObject directTarget)) return directTarget;

        // Collect all GameObjects and their child transforms
        List<GameObject> allObjects = new List<GameObject>();
        foreach (var rootObj in map.Values)
        {
            if (rootObj == null) continue;
            if (!allObjects.Contains(rootObj)) allObjects.Add(rootObj);
            foreach (Transform t in rootObj.GetComponentsInChildren<Transform>(true))
            {
                if (t != null && !allObjects.Contains(t.gameObject))
                {
                    allObjects.Add(t.gameObject);
                }
            }
        }

        // 2. Second priority: Exact case-insensitive match
        foreach (var obj in allObjects)
        {
            if (string.Equals(obj.name, compName, StringComparison.OrdinalIgnoreCase))
                return obj;
        }

        // 3. Third priority: Suffix/prefix match (e.g. glTF importer appends suffixes)
        string cleanComp = CleanComponentName(compName);
        foreach (var obj in allObjects)
        {
            string cleanObj = CleanComponentName(obj.name);
            if (string.Equals(cleanObj, cleanComp, StringComparison.OrdinalIgnoreCase))
                return obj;
        }

        // 4. Fourth priority: Loose containment
        foreach (var obj in allObjects)
        {
            if (obj.name.ToLower().Contains(compName.ToLower()))
                return obj;
        }

        return null;
    }

    private string CleanComponentName(string name)
    {
        if (string.IsNullOrEmpty(name)) return "";
        string clean = name;
        string[] noise = { "_mesh", "_node", "_primitive", "_instance" };
        foreach (var n in noise)
        {
            int idx = clean.ToLower().IndexOf(n);
            if (idx > 0) clean = clean.Substring(0, idx);
        }
        int dotIdx = clean.IndexOf('.');
        if (dotIdx > 0) clean = clean.Substring(0, dotIdx);
        return clean.Trim().ToLower();
    }

    IEnumerator UpdateAASPin(SensorBinding binding, bool state, bool isOverlapping, GameObject sensorObj, GameObject productObj)
    {
        string pinId = binding.pinId;
        Debug.Log($"[Sensor] 📡 Updating {pinId} to {state}");

        // Track last exited product from local simulator
        if (!state && productObj != null)
        {
            var rb = productObj.GetComponentInParent<Rigidbody>();
            if (rb != null)
            {
                lastDetectedProduct = rb;
                Debug.Log($"[Sensor] 🎯 Last exited product registered locally: '{rb.gameObject.name}'");
            }
        }

        // --- SNAP / TELEPORT LOGIC ---
        // If the sensor has a Snap configuration and the product is physically entering (isOverlapping is true),
        // we start a coroutine to snap it after a 0.3s delay.
        // Includes a 1.5s cooldown to prevent infinite snapping while a conveyor is running
        if (isOverlapping && binding.hasSnap && (UnityEngine.Time.time - binding.lastSnapTime > 1.5f))
        {
            binding.lastSnapTime = UnityEngine.Time.time;
            StartCoroutine(SnapWithDelayRoutine(binding, sensorObj, productObj, pinId));
        }

        // BaSyx 2.0 requires dot notation for nested elements inside collections
        string url = $"{pinsUrl}.{pinId}";
        string json = $"{{\"idShort\":\"{pinId}\",\"modelType\":\"Property\",\"valueType\":\"xs:boolean\",\"value\":\"{state.ToString().ToLower()}\"}}";
        
        using (UnityWebRequest req = new UnityWebRequest(url, "PUT"))
        {
            byte[] bodyRaw = System.Text.Encoding.UTF8.GetBytes(json);
            req.uploadHandler = new UploadHandlerRaw(bodyRaw);
            req.downloadHandler = new DownloadHandlerBuffer();
            req.SetRequestHeader("Content-Type", "application/json");
            req.timeout = 2;

            yield return req.SendWebRequest();
            if (req.result != UnityWebRequest.Result.Success)
                Debug.LogWarning($"[Sensor] ❌ Failed to update {pinId}: {req.error} (URL: {url})");
        }
    }

    // -------------------------------------------------------------------------
    // Move-on-state: moves a named component to a target local pose, carrying
    // any non-kinematic Rigidbodies that are physically overlapping it.
    // -------------------------------------------------------------------------
    private IEnumerator ExecuteMoveRoutine(SensorBinding binding)
    {
        Debug.Log($"[DT-LOG] 🔀 ExecuteMoveRoutine started for moveTargetName='{binding.moveTargetName}' (pinId='{binding.pinId}')");
        GameObject targetObj = FindTargetComponent(_componentMap, binding.moveTargetName);
        if (targetObj == null)
        {
            Debug.LogWarning($"[DT-LOG] ⚠️ MoveOnState: target component '{binding.moveTargetName}' not found.");
            yield break;
        }

        Vector3 targetLocalPos = binding.moveTargetLocalPos;
        Quaternion targetLocalRot = Quaternion.Euler(binding.moveTargetLocalRot);

        bool doPosition = binding.moveMode != "Rotation";
        bool doRotation = binding.moveMode != "Position";

        Debug.Log($"[DT-LOG] 🔀 ExecuteMoveRoutine config: moveMode={binding.moveMode}, doPos={doPosition}, doRot={doRotation}, targetPos={targetLocalPos}, targetRot={binding.moveTargetLocalRot}, duration={binding.moveDuration}. Current pose: pos={targetObj.transform.localPosition}, rot={targetObj.transform.localRotation.eulerAngles}");

        // Record dynamic products overlapping the target BEFORE the move so we can carry them
        var products = FindDynamicProductsOn(targetObj);
        var relStates = new List<(Rigidbody rb, Vector3 relPos, Quaternion relRot)>();
        foreach (var rb in products)
        {
            Vector3 relPos = targetObj.transform.InverseTransformPoint(rb.position);
            Quaternion relRot = Quaternion.Inverse(targetObj.transform.rotation) * rb.rotation;
            relStates.Add((rb, relPos, relRot));
        }

        void ApplyToProducts()
        {
            foreach (var (rb, relPos, relRot) in relStates)
            {
                if (rb == null) continue;
                rb.position = targetObj.transform.TransformPoint(relPos);
                rb.rotation = targetObj.transform.rotation * relRot;
            }
        }

        if (binding.moveDuration <= 0f)
        {
            // Instant snap
            if (doPosition) targetObj.transform.localPosition = targetLocalPos;
            if (doRotation) targetObj.transform.localRotation = targetLocalRot;
            ApplyToProducts();
            foreach (var (rb, _, _) in relStates)
                if (rb != null) { rb.linearVelocity = Vector3.zero; rb.angularVelocity = Vector3.zero; }
        }
        else
        {
            // Smooth lerp
            float elapsed = 0f;
            Vector3 startPos = targetObj.transform.localPosition;
            Quaternion startRot = targetObj.transform.localRotation;

            while (elapsed < binding.moveDuration)
            {
                elapsed += Time.deltaTime;
                float t = Mathf.SmoothStep(0f, 1f, Mathf.Clamp01(elapsed / binding.moveDuration));
                if (doPosition) targetObj.transform.localPosition = Vector3.Lerp(startPos, targetLocalPos, t);
                if (doRotation) targetObj.transform.localRotation = Quaternion.Slerp(startRot, targetLocalRot, t);
                ApplyToProducts();
                yield return null;
            }

            // Snap to exact final values
            if (doPosition) targetObj.transform.localPosition = targetLocalPos;
            if (doRotation) targetObj.transform.localRotation = targetLocalRot;
            ApplyToProducts();
            foreach (var (rb, _, _) in relStates)
                if (rb != null) { rb.linearVelocity = Vector3.zero; rb.angularVelocity = Vector3.zero; }
        }

        Debug.Log($"[Sensor] 🔀 MoveOnState: moved '{binding.moveTargetName}' (Mode: {binding.moveMode}) → pos={targetLocalPos}, rot={binding.moveTargetLocalRot} (trigger state={binding.moveOnState}, carrying {relStates.Count} product(s))");
        binding.activeMoveCoroutine = null;
    }

    // Finds all non-kinematic Rigidbodies physically overlapping any collider of 'target'.
    private List<Rigidbody> FindDynamicProductsOn(GameObject target)
    {
        var results = new List<Rigidbody>();
        var seen = new HashSet<Rigidbody>();
        foreach (var col in target.GetComponentsInChildren<Collider>())
        {
            if (col == null || col.isTrigger) continue;
            Bounds b = col.bounds;
            b.Expand(0.06f); // slightly inflate to catch objects resting on top
            Collider[] overlapping = Physics.OverlapBox(b.center, b.extents, target.transform.rotation);
            foreach (var other in overlapping)
            {
                if (other == col) continue;
                // Skip colliders that are children of the same object
                if (other.transform.IsChildOf(target.transform)) continue;
                var rb = other.GetComponentInParent<Rigidbody>();
                if (rb != null && !rb.isKinematic && !seen.Contains(rb))
                {
                    seen.Add(rb);
                    results.Add(rb);
                }
            }
        }
        return results;
    }


    IEnumerator SnapWithDelayRoutine(SensorBinding binding, GameObject sensorObj, GameObject productObj, string pinId)
    {
        // Wait for 0.3 seconds to let Python turn off the conveyor and let the product settle
        yield return new WaitForSeconds(0.3f);

        GameObject product = productObj;
        Rigidbody rb = null;
        if (product != null)
        {
            Rigidbody parentRb = product.GetComponentInParent<Rigidbody>();
            if (parentRb != null)
            {
                rb = parentRb;
                product = parentRb.gameObject;
            }
            else
            {
                rb = product.GetComponent<Rigidbody>();
            }
        }

        if (product == null && !string.IsNullOrEmpty(binding.snapTarget))
        {
            product = GameObject.Find(binding.snapTarget);
            if (product != null)
            {
                rb = product.GetComponent<Rigidbody>();
            }
        }

        if (product != null)
        {
            Vector3 unityPos;
            float targetY = (product.transform.position.y < -1.0f) ? (sensorObj.transform.position.y + 0.05f) : product.transform.position.y;

            if (binding.snapPosition.sqrMagnitude < 0.0001f)
            {
                unityPos = new Vector3(sensorObj.transform.position.x, targetY, sensorObj.transform.position.z);
            }
            else
            {
                unityPos = new Vector3(binding.snapPosition.x, binding.snapPosition.y, -binding.snapPosition.z);
            }
            
            float dist = Vector3.Distance(product.transform.position, unityPos);
            float angle = Quaternion.Angle(product.transform.rotation, Quaternion.Euler(0, sensorObj.transform.eulerAngles.y, 0));
            
            if (dist > 0.02f || angle > 2f)
            {
                product.transform.position = unityPos;
                
                // Also align rotation to match the sensor's orientation (along the conveyor axis)
                product.transform.rotation = Quaternion.Euler(0, sensorObj.transform.eulerAngles.y, 0);
                
                // Reset velocity and clear inertia to avoid physical "bouncing" after teleport
                if (rb != null) {
                    bool wasKinematic = rb.isKinematic;
                    rb.isKinematic = true;
                    rb.isKinematic = wasKinematic;

                    rb.linearVelocity = Vector3.zero;
                    rb.angularVelocity = Vector3.zero;
                    rb.ResetCenterOfMass();
                    rb.ResetInertiaTensor();
                    Physics.SyncTransforms();
                }
                
                Debug.Log($"[Sensor] ⚡ DELAYED SNAP! Teleported dynamic product '{product.name}' to {unityPos} via {pinId} (Dist={dist:F4}, Angle={angle:F2}º) - Physics Reset");
            }
        }
    }

    // Helper class to be attached to each sensor component
    public class SensorPhysicsProxy : MonoBehaviour
    {
        public SensorBinding binding;
        public Action<bool, bool, GameObject, GameObject> onStateChange;
        public HashSet<Collider> touching = new HashSet<Collider>();

        // Delay the first state check to allow dynamic objects to be positioned
        // by the AAS position sync loop before sensors report their initial state.
        public float startupStabilizationDelay = 1.5f;

        void Start() {
            StartCoroutine(DelayedInitialCheck());
        }

        // Called by ApplyOperationMode when switching back to VC mode.
        // Re-enables the proxy and re-runs the initial state check after the delay.
        public void ResetAndEnable()
        {
            touching.Clear();
            this.enabled = true;
            StartCoroutine(DelayedInitialCheck());
        }

        private IEnumerator DelayedInitialCheck()
        {
            yield return new WaitForSeconds(startupStabilizationDelay);
            CheckState(true);
        }

        private void OnTriggerEnter(Collider other)
        {
            if (binding.type != "TriggerZone" && binding.type != "ContactSensor") return;
            if (MatchesFilter(other))
            {
                touching.Add(other);
                CheckState();
            }
        }

        private void OnTriggerExit(Collider other)
        {
            if (binding.type != "TriggerZone" && binding.type != "ContactSensor") return;
            if (MatchesFilter(other))
            {
                touching.Remove(other);
                CheckState();
            }
        }

        private void OnCollisionEnter(Collision collision)
        {
            if (binding.type != "ContactSensor") return;
            if (MatchesFilter(collision.collider))
            {
                touching.Add(collision.collider);
                CheckState();
            }
        }

        private void OnCollisionExit(Collision collision)
        {
            if (binding.type != "ContactSensor") return;
            if (MatchesFilter(collision.collider))
            {
                touching.Remove(collision.collider);
                CheckState();
            }
        }

        private bool MatchesFilter(Collider other)
        {
            if (string.IsNullOrEmpty(binding.targetFilter)) return true;
            
            string n = other.gameObject.name;
            string t = other.gameObject.tag;
            string filter = binding.targetFilter;

            // If the filter is "Metal" (product sensor), it must only match dynamic products
            if (filter.Equals("Metal", StringComparison.OrdinalIgnoreCase))
            {
                bool isProduct = other.GetComponentInParent<AAS.DynamicProduct>() != null || 
                                 other.GetComponentInChildren<AAS.DynamicProduct>() != null;
                if (!isProduct) return false;
            }

            bool nameMatch = n.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0;
            bool tagMatch = t.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0;
            
            bool parentMatch = false;
            Transform p = other.transform.parent;
            while(p != null) {
                if(p.name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0) {
                    parentMatch = true;
                    break;
                }
                p = p.parent;
            }

            return nameMatch || tagMatch || parentMatch;
        }

        private void CheckState(bool force = false)
        {
            touching.RemoveWhere(c => c == null);
            bool isOverlapping = touching.Count > 0;
            bool newState = binding.activeLow ? !isOverlapping : isOverlapping;
            
            if (force || newState != binding.lastState)
            {
                binding.lastState = newState;
                Collider triggeringCol = touching.FirstOrDefault(c => c != null);
                GameObject triggeringProduct = triggeringCol != null ? triggeringCol.gameObject : null;
                onStateChange?.Invoke(newState, isOverlapping, this.gameObject, triggeringProduct);
            }
        }
    }


 
    string Base64Url(string i) => Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(i)).Replace("+", "-").Replace("/", "_").TrimEnd('=');
}