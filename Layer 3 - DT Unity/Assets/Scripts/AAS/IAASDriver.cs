using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.Networking;
using Newtonsoft.Json.Linq;

public interface IAASDriver
{
    void WriteActuator(string kitId, string pinId, bool value);
    bool ReadSensor(string kitId, string pinId, bool useReal = false);
    bool ReadSensorFromAAS(string kitId, string pinId);
}

public class AASHttpDriver : IAASDriver
{
    private AASSensorController sensorController;
    private AASActuatorController actuatorController;
    private string baseRepoUrl;
    private string virtualPinsSubmodelId;

    public AASHttpDriver(
        AASSensorController sensorController,
        AASActuatorController actuatorController,
        string baseRepoUrl,
        string virtualPinsSubmodelId)
    {
        this.sensorController = sensorController;
        this.actuatorController = actuatorController;
        this.baseRepoUrl = baseRepoUrl;
        this.virtualPinsSubmodelId = virtualPinsSubmodelId;
    }

    public void WriteActuator(string kitId, string pinId, bool value)
    {
        GameObject kitGo = FindKitGameObject(kitId);
        if (kitGo != null)
        {
            var actCtrl = kitGo.GetComponent<AASActuatorController>();
            if (actCtrl != null && actCtrl.bindings != null)
            {
                var binding = actCtrl.bindings.Find(b => b.pinId == pinId);
                if (binding != null)
                {
                    binding.currentState = value;
                }
                else
                {
                    Debug.LogWarning($"[AASHttpDriver] Actuator with pinId '{pinId}' not found in bindings of kit '{kitId}'.");
                }
            }
            else
            {
                Debug.LogWarning($"[AASHttpDriver] AASActuatorController not found or bindings null on kit '{kitId}'.");
            }
        }
        else
        {
            Debug.LogWarning($"[AASHttpDriver] Kit GameObject '{kitId}' not found in scene for WriteActuator.");
        }

        // Skip updating AAS via HTTP PUT when in Digital Twin mode (visual-only local feedback)
        if (OperationModeManager.Current != OperationMode.DigitalTwin)
        {
            StaticCoroutineRunner.Start(UpdateActuatorAAS(kitId, pinId, value));
        }
    }

    public bool ReadSensor(string kitId, string pinId, bool useReal = false)
    {
        GameObject kitGo = FindKitGameObject(kitId);
        if (kitGo != null)
        {
            var controller = kitGo.GetComponent<AASSensorController>();
            if (controller != null && controller.GetSensorState(pinId, out bool state, useReal))
            {
                return state;
            }

            var proxies = kitGo.GetComponentsInChildren<AASSensorController.SensorPhysicsProxy>(true);
            foreach (var proxy in proxies)
            {
                if (proxy != null && proxy.binding != null && proxy.binding.pinId == pinId)
                {
                    return useReal ? proxy.binding.realState : proxy.binding.lastState;
                }
            }
        }

        // Fallback: If not found locally in the scene, perform synchronous HTTP GET request
        return ReadSensorFromAAS(kitId, pinId);
    }

    private GameObject FindKitGameObject(string kitId)
    {
        if (string.IsNullOrEmpty(kitId)) return null;

        // Try exact match first
        GameObject exact = GameObject.Find(kitId);
        if (exact != null) return exact;

        // Try exact match with Clone suffix
        GameObject exactClone = GameObject.Find(kitId + "(Clone)");
        if (exactClone != null) return exactClone;

        // Try case-insensitive search but prefer objects without "LogicalAAS_" prefix
        var allGo = UnityEngine.Object.FindObjectsByType<GameObject>(FindObjectsSortMode.None);
        GameObject fallback = null;
        foreach (var go in allGo)
        {
            if (go != null && go.name.IndexOf(kitId, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                if (go.name.StartsWith("LogicalAAS_"))
                {
                    if (fallback == null) fallback = go;
                }
                else
                {
                    return go;
                }
            }
        }
        return fallback;
    }

    public bool ReadSensorFromAAS(string kitId, string pinId)
    {
        string base64SubmodelId = Base64Url("https://acplt.org/Submodels/Pins_" + kitId);
        // Inputs = sensor readings (what enters the PLC/system)
        string url = $"{baseRepoUrl}/submodels/{base64SubmodelId}/submodel-elements/Inputs.{pinId}";
        try
        {
            var request = (System.Net.HttpWebRequest)System.Net.WebRequest.Create(url);
            request.Method = "GET";
            request.Timeout = 2000; // 2 seconds
            using (var response = (System.Net.HttpWebResponse)request.GetResponse())
            {
                using (var stream = response.GetResponseStream())
                {
                    using (var reader = new System.IO.StreamReader(stream))
                    {
                        string json = reader.ReadToEnd();
                        JObject obj = JObject.Parse(json);
                        string valStr = obj["value"]?.ToString();
                        if (bool.TryParse(valStr, out bool result))
                        {
                            return result;
                        }
                        if (valStr == "1") return true;
                        if (valStr == "0") return false;
                        if (string.Equals(valStr, "true", StringComparison.OrdinalIgnoreCase)) return true;
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Debug.LogWarning($"[AASHttpDriver] Synchronous HTTP GET failed for sensor '{pinId}' on kit '{kitId}' (URL: {url}): {ex.Message}");
        }
        return false;
    }

    private IEnumerator UpdateActuatorAAS(string kitId, string pinId, bool value)
    {
        string base64SubmodelId = Base64Url("https://acplt.org/Submodels/Pins_" + kitId);
        // Outputs = actuator commands (what the PLC/system drives)
        string url = $"{baseRepoUrl}/submodels/{base64SubmodelId}/submodel-elements/Outputs.{pinId}";
        string json = $"{{\"idShort\":\"{pinId}\",\"modelType\":\"Property\",\"valueType\":\"xs:boolean\",\"value\":\"{value.ToString().ToLower()}\"}}";

        using (UnityWebRequest req = new UnityWebRequest(url, "PUT"))
        {
            byte[] bodyRaw = System.Text.Encoding.UTF8.GetBytes(json);
            req.uploadHandler = new UploadHandlerRaw(bodyRaw);
            req.downloadHandler = new DownloadHandlerBuffer();
            req.SetRequestHeader("Content-Type", "application/json");
            req.timeout = 2;

            yield return req.SendWebRequest();
            if (req.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning($"[AASHttpDriver] ❌ Failed to write actuator {pinId} on kit {kitId}: {req.error} (URL: {url})");
            }
        }
    }

    private string Base64Url(string i)
    {
        if (string.IsNullOrEmpty(i)) return "";
        return Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(i))
            .Replace("+", "-")
            .Replace("/", "_")
            .TrimEnd('=');
    }
}

public class StaticCoroutineRunner : MonoBehaviour
{
    private static StaticCoroutineRunner instance;

    public static StaticCoroutineRunner Instance
    {
        get
        {
            if (instance == null)
            {
                GameObject go = new GameObject("StaticCoroutineRunner");
                instance = go.AddComponent<StaticCoroutineRunner>();
                DontDestroyOnLoad(go);
            }
            return instance;
        }
    }

    public static Coroutine Start(IEnumerator routine)
    {
        return Instance.StartCoroutine(routine);
    }
}
