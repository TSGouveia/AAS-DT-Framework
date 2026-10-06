using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using System.Linq;
using UnityEngine;
using UnityEngine.Networking;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Unity.Robotics.UrdfImporter;

public class AASRobotSync : MonoBehaviour
{
    private string submodelUrl;
    private string aasServerUrl;
    
    [Header("Telemetry State")]
    public string units = "rad"; // "rad", "deg"

    [Header("Smoothing")]
    public float lerpSpeed = 1f;

    [Serializable]
    public class JointData {
        public Transform transform;
        public Quaternion initialRotation;
        public Vector3 axis;
        public float currentAngle;
        public float targetAngle;

        [Header("Calibration")]
        public float angleOffset = 0f;
        public bool invert = false;
    }

    public struct JointInitData {
        public string name;
        public Transform transform;
        public Vector3 axis;
    }

    private Dictionary<string, JointData> jointMap = new Dictionary<string, JointData>();
    private HashSet<string> loggedJoints = new HashSet<string>();
    private float updateRate = 0.02f; // 50Hz for faster sync
    private float lastUpdateTime = 0;

    public void SetJointMap(List<JointInitData> joints)
    {
        jointMap.Clear();
        string names = "";
        foreach (var joint in joints)
        {
            string key = SanitizeAasId(joint.name).ToLower();
            jointMap[key] = new JointData { 
                transform = joint.transform,
                initialRotation = joint.transform.localRotation,
                axis = joint.axis,
                currentAngle = 0f,
                targetAngle = 0f
            };
            names += key + ", ";
        }
        Debug.Log($"[Sync] 🗺️ Joint map received: {jointMap.Count} joints. Targets: {names}");
    }

    public void Init(string submodelId, string serverUrl)
    {
        this.aasServerUrl = serverUrl;
        string encodedId = Base64Url(submodelId);
        this.submodelUrl = $"{serverUrl}/submodels/{encodedId}";
        Debug.Log($"[Sync] 🛠️ Initializing Sync for {gameObject.name}. Server: {serverUrl}, Submodel: {submodelId}");
        
        StartCoroutine(FetchTelemetryConfig());
    }

    IEnumerator FetchTelemetryConfig()
    {
        string configUrl = $"{submodelUrl}/submodel-elements?level=deep";
        using (UnityWebRequest req = UnityWebRequest.Get(configUrl))
        {
            yield return req.SendWebRequest();

            if (req.result == UnityWebRequest.Result.Success)
            {
                List<BaSyxManager.SubmodelElement> elements = ParseElements(req.downloadHandler.text);
                if (elements != null)
                {
                    foreach (var el in elements)
                    {
                        if (el.idShort == "JointUnits") units = el.value?.ToString();
                    }
                    Debug.Log($"[Sync] ⚙️ Configured: Units={units}");
                    StartCoroutine(CommunicationLoop());
                }
            }
        }
    }

    private List<BaSyxManager.SubmodelElement> ParseElements(string json)
    {
        try {
            JToken token = JToken.Parse(json);
            if (token is JArray arr) return arr.ToObject<List<BaSyxManager.SubmodelElement>>();
            if (token is JObject obj) {
                if (obj["result"] != null) return obj["result"].ToObject<List<BaSyxManager.SubmodelElement>>();
                if (obj["value"] != null) {
                    var val = obj["value"];
                    if (val is JArray vArr) return vArr.ToObject<List<BaSyxManager.SubmodelElement>>();
                    // Se for objeto individual, tentar converter para lista
                    try { return new List<BaSyxManager.SubmodelElement> { val.ToObject<BaSyxManager.SubmodelElement>() }; } catch {}
                }
            }
        } catch {}
        return null;
    }

    IEnumerator CommunicationLoop()
    {
        Debug.Log($"[Sync] 🚀 Starting Live Polling for {gameObject.name}...");
        while (true)
        {
            if (Time.time - lastUpdateTime >= updateRate)
            {
                lastUpdateTime = Time.time;
                yield return StartCoroutine(SyncFromAASLiveJoints());
            }
            yield return null; 
        }
    }

    IEnumerator SyncFromAASLiveJoints()
    {
        string url = $"{submodelUrl}/submodel-elements/LiveJoints?level=deep";
        using (UnityWebRequest req = UnityWebRequest.Get(url))
        {
            req.timeout = 1;
            yield return req.SendWebRequest();

            if (req.result == UnityWebRequest.Result.Success)
            {
                var elements = ParseElements(req.downloadHandler.text);
                if (elements != null)
                {
                    foreach (var el in elements)
                    {
                        if (el == null || string.IsNullOrEmpty(el.idShort)) continue;
                        
                        string key = el.idShort.ToLower();
                        if (el.value != null) {
                            if (jointMap.TryGetValue(key, out JointData data))
                            {
                                float val = SafeFloat(el.value);
                                if (!loggedJoints.Contains(key)) {
                                    Debug.Log($"[Sync] ✅ Updating joint '{key}' with value: {val} ({units})");
                                    loggedJoints.Add(key);
                                }
                                UpdateTargetAngle(data, val);
                            }
                            else if (Time.frameCount % 2000 == 0) {
                                Debug.LogWarning($"[Sync] ❓ AAS joint '{key}' not found in Unity map.");
                            }
                        }
                    }
                }
            }
        }
    }

    void Update()
    {
        foreach (var kvp in jointMap)
        {
            JointData data = kvp.Value;
            
            // Suavização do movimento
            data.currentAngle = Mathf.LerpAngle(data.currentAngle, data.targetAngle, Time.deltaTime * lerpSpeed);
            
            // AplicaÃ§Ã£o da rotaÃ§Ã£o baseada na pose inicial URDF
            // Invert the angle to match Unity's left-handed system relative to ROS right-handed system
            data.transform.localRotation = data.initialRotation * Quaternion.AngleAxis(-data.currentAngle, data.axis);
            }
    }

    float SafeFloat(object v)
    {
        if (v == null) return 0f;
        if (v is float f) return f;
        if (v is double d) return (float)d;
        if (v is int i) return (float)i;
        if (v is long l) return (float)l;
        if (v is string s && float.TryParse(s, System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out float res)) return res;
        try { return Convert.ToSingle(v, System.Globalization.CultureInfo.InvariantCulture); }
        catch { return 0f; }
    }

    void UpdateTargetAngle(JointData data, float value)
    {
        float angle = (units == "deg") ? value : value * Mathf.Rad2Deg;
        data.targetAngle = (angle * (data.invert ? -1f : 1f)) + data.angleOffset;
    }

    string SanitizeAasId(string name) {
        return System.Text.RegularExpressions.Regex.Replace(name, @"[^a-zA-Z0-9_]", "_");
    }

    string Base64Url(string i) => Convert.ToBase64String(Encoding.UTF8.GetBytes(i)).Replace("+", "-").Replace("/", "_").TrimEnd('=');
}
