using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.Networking;
using Newtonsoft.Json.Linq;
using TMPro;

public class AASRuleEngine : MonoBehaviour
{
    [Header("Error / Warning UI (DT Mode)")]
    public GameObject warningPanel;
    public TextMeshProUGUI warningTextTMP;

    private IAASDriver driver;
    private string rulesUrl;
    private string _skillServerURL = ""; // Base URL for DT skill invocation (from AAS AssetInterfacesDescription)
    private bool isInitialized = false;

    [Tooltip("Seconds to wait before starting trigger evaluation.")]
    public float startupStabilizationDelay = 10.0f;

    private JArray startSteps;
    private JArray sequences;
    public HashSet<string> activeSequences = new HashSet<string>();
    private Dictionary<string, object> variables = new Dictionary<string, object>();

    /// <param name="skillServerURL">
    /// Base URL of the skill server used in Digital Twin mode (e.g. "http://192.168.10.1:80").
    /// When a trigger fires in DT mode, Unity calls GET {skillServerURL}/skills/{sequenceName}
    /// and the server is responsible for driving the real hardware.
    /// </param>
    public void Init(string serverUrl, string controlLogicSubmodelId, IAASDriver aasDriver, string skillServerURL = "", float delay = 10.0f)
    {
        this.driver          = aasDriver;
        this._skillServerURL = skillServerURL ?? "";
        this.rulesUrl        = $"{serverUrl}/submodels/{Base64Url(controlLogicSubmodelId)}/submodel-elements/Rules";
        this.startupStabilizationDelay = delay;
        
        activeSequences.Clear();
        variables.Clear();
        
        // Listen for mode changes to clear executing sequences and prevent lockups
        OperationModeManager.OnModeChanged += HandleModeChanged;
        
        StartCoroutine(FetchAndStartRulesRoutine());
    }

    void OnDestroy()
    {
        OperationModeManager.OnModeChanged -= HandleModeChanged;
    }

    private void HandleModeChanged(OperationMode newMode)
    {
        Debug.Log($"[RuleEngine] 🔄 Operation mode changed to {newMode}. Stopping active sequences to reset state.");
        StopAllCoroutines();
        activeSequences.Clear();
        
        // Restart rules fetching and evaluation loop under the new mode
        isInitialized = false;
        StartCoroutine(FetchAndStartRulesRoutine());
    }

    private IEnumerator FetchAndStartRulesRoutine()
    {
        // VC startup protection: force reset all actuators in the scene to false IMMEDIATELY on scene startup
        if (OperationModeManager.Current == OperationMode.VirtualCommissioning)
        {
            var allActuatorControllers = FindObjectsByType<AASActuatorController>(FindObjectsSortMode.None);
            foreach (var actCtrl in allActuatorControllers)
            {
                if (actCtrl != null && actCtrl.bindings != null)
                {
                    string kitName = actCtrl.gameObject.name;
                    if (kitName.EndsWith("(Clone)"))
                    {
                        kitName = kitName.Substring(0, kitName.Length - 7).Trim();
                    }
                    foreach (var act in actCtrl.bindings)
                    {
                        if (act != null && !string.IsNullOrEmpty(act.pinId))
                        {
                            driver.WriteActuator(kitName, act.pinId, false);
                            act.currentState = false;
                            Debug.Log($"[RuleEngine] [Startup Reset] Forced actuator '{act.pinId}' on kit '{kitName}' -> false");
                        }
                    }
                }
            }
        }

        // Wait until AASSensorController has completed its initial DT synchronization burst
        var sensorCtrl = GetComponent<AASSensorController>();
        if (sensorCtrl != null)
        {
            while (!sensorCtrl.IsInitialDTSyncComplete())
            {
                Debug.Log($"[RuleEngine] ⏳ Waiting for initial DT sensor synchronization on '{gameObject.name}'...");
                yield return new WaitForSeconds(0.2f);
            }
        }

        // Wait for the system to stabilize: dynamic objects need to be positioned
        // by the AAS sync loop and sensors need to do their initial state check.
        Debug.Log($"[RuleEngine] ⏳ Waiting {startupStabilizationDelay}s for system to stabilize before starting rules...");
        yield return new WaitForSeconds(startupStabilizationDelay);

        Debug.Log($"[RuleEngine] 🔄 Fetching rules from {rulesUrl}");
        using (UnityWebRequest req = UnityWebRequest.Get(rulesUrl))
        {
            yield return req.SendWebRequest();

            if (req.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning($"[RuleEngine] ⚠️ No logic rules found or failed to fetch: {req.error}");
                yield break;
            }

            try
            {
                JObject root = JObject.Parse(req.downloadHandler.text);
                string rulesJsonStr = root["value"]?.ToString();
                if (string.IsNullOrEmpty(rulesJsonStr))
                {
                    rulesJsonStr = root.ToString();
                }

                JObject rules = JObject.Parse(rulesJsonStr);
                startSteps = rules["start"] as JArray;
                sequences = rules["sequences"] as JArray;

                Debug.Log($"[RuleEngine] 📜 AST Rules loaded. Start actions: {startSteps?.Count ?? 0}, Sequences: {sequences?.Count ?? 0}");

                isInitialized = true;

                // Run Start Sequence
                if (startSteps != null && startSteps.Count > 0)
                {
                    StartCoroutine(ExecuteSequenceRoutine("Start", startSteps));
                }

                // Begin evaluation loop for triggered sequences
                StartCoroutine(TriggerEvaluationLoop());
            }
            catch (Exception e)
            {
                Debug.LogError($"[RuleEngine] ❌ Error parsing rules JSON: {e.Message}");
            }
        }
    }

    private IEnumerator TriggerEvaluationLoop()
    {
        while (isInitialized)
        {
            if (sequences != null)
            {
                foreach (JObject seq in sequences)
                {
                    string name = seq["name"]?.ToString() ?? "UnnamedSequence";
                    if (activeSequences.Contains(name)) continue;

                    JToken trigger = seq["trigger"];
                    if (trigger != null)
                    {
                        bool condSim = EvaluateCondition(trigger, forceRealAAS: false, useRealState: false);
                        bool condReal = EvaluateCondition(trigger, forceRealAAS: false, useRealState: true);

                        // In DT mode, trigger sequence if EITHER simulated OR real condition is met.
                        // The subsequent wait_until steps inside the sequence will enforce strict AND synchronization.
                        bool condMet = (OperationModeManager.Current == OperationMode.DigitalTwin) ? (condSim || condReal) : condSim;
                        if (!condMet) continue;
                    }

                    JArray steps = seq["steps"] as JArray;

                    if (OperationModeManager.Current == OperationMode.DigitalTwin)
                    {
                        StartCoroutine(ExecuteDTSequenceWithSkillRoutine(name, steps));
                    }
                    else
                    {
                        // VC mode: Unity is the PLC — run sequence fully locally.
                        if (steps != null)
                            StartCoroutine(ExecuteSequenceRoutine(name, steps, dtMode: false));
                    }
                }
            }
            yield return new WaitForSeconds(0.1f);
        }
    }

    // Invokes a skill on the configured skill server via HTTP GET.
    // Generic URL: {_skillServerURL}/skills/{sequenceName}
    // The skill server maps this to whatever real-hardware action is needed.
    private IEnumerator InvokeSkillRoutine(string sequenceName)
    {
        string url = $"{_skillServerURL}/skills/{Uri.EscapeDataString(sequenceName)}";
        Debug.Log($"[RuleEngine] 🔗 DT skill invoke: {url}");
        using var req = UnityWebRequest.Get(url);
        req.timeout = 30;
        yield return req.SendWebRequest();
        if (req.result == UnityWebRequest.Result.Success)
            Debug.Log($"[RuleEngine] ✅ Skill '{sequenceName}': {req.downloadHandler.text}");
        else
            Debug.LogWarning($"[RuleEngine] ❌ Skill '{sequenceName}' failed: {req.error}");
    }

    private IEnumerator ExecuteDTSequenceWithSkillRoutine(string seqName, JArray steps)
    {
        activeSequences.Add(seqName);
        Debug.Log($"[RuleEngine] 🚀 Sequence '{seqName}' [DT Mode - Real + Simulator]");

        Coroutine skillCoroutine = null;
        if (!string.IsNullOrEmpty(_skillServerURL) && _skillServerURL != "none" && !_skillServerURL.Equals("http://:80"))
        {
            skillCoroutine = StartCoroutine(InvokeSkillRoutine(seqName));
        }

        Coroutine simCoroutine = null;
        if (steps != null)
        {
            simCoroutine = StartCoroutine(ExecuteBlockRoutine(seqName, steps, dtMode: true));
        }

        if (skillCoroutine != null) yield return skillCoroutine;
        if (simCoroutine != null) yield return simCoroutine;

        Debug.Log($"[RuleEngine] ✅ Sequence complete on both Real and Simulator: '{seqName}'");
        activeSequences.Remove(seqName);
    }

    private IEnumerator ExecuteSequenceRoutine(string seqName, JArray steps, bool dtMode = false)
    {
        activeSequences.Add(seqName);
        Debug.Log($"[RuleEngine] 🚀 Sequence '{seqName}' [{(dtMode ? "DT-visual" : "VC-full")}]");

        yield return StartCoroutine(ExecuteBlockRoutine(seqName, steps, dtMode));

        Debug.Log($"[RuleEngine] ✅ Sequence complete: '{seqName}'");
        activeSequences.Remove(seqName);
    }

    private IEnumerator ExecuteBlockRoutine(string seqName, JArray steps, bool dtMode = false)
    {
        if (steps == null) yield break;

        foreach (JToken stepToken in steps)
        {
            if (!(stepToken is JObject step)) continue;

            string type = step["type"]?.ToString();

            switch (type)
            {
                case "set_actuator":
                {
                    string actuator = step["actuator"]?.ToString();
                    string kit = step["kit"]?.ToString();
                    if (string.IsNullOrEmpty(kit)) kit = GetLocalKitName();
                    bool value = step["value"]?.ToObject<bool>() ?? false;
                    driver.WriteActuator(kit, actuator, value);
                    Debug.Log($"[RuleEngine] [{seqName}] Actuator '{actuator}' on kit '{kit}' -> {value} (Local simulator drive)");
                    break;
                }

                case "wait_time":
                {
                    float seconds = step["seconds"]?.ToObject<float>() ?? 0f;
                    yield return new WaitForSeconds(seconds);
                    break;
                }

                 case "wait_until":
                 {
                     JToken condToken = step["condition"] ?? step;
 
                     while (true)
                     {
                         bool condSim = EvaluateCondition(condToken, forceRealAAS: false, useRealState: false);
                         bool condReal = EvaluateCondition(condToken, forceRealAAS: false, useRealState: true);
                         
                         if (dtMode)
                         {
                             if (condReal)
                             {
                                 // Immediate Sync Snap to keep simulator in sync with real hardware
                                 string sensorName = condToken["sensor"]?.ToString();
                                 string kitName = condToken["kit"]?.ToString();
                                 if (!string.IsNullOrEmpty(sensorName))
                                 {
                                     if (string.IsNullOrEmpty(kitName)) kitName = GetLocalKitName();
                                     GameObject kitGo = GameObject.Find(kitName);
                                     if (kitGo != null)
                                     {
                                         var sensorCtrl = kitGo.GetComponent<AASSensorController>();
                                         if (sensorCtrl != null)
                                         {
                                             var resolvedSensorObj = sensorCtrl.transform.Find(sensorName)?.gameObject;
                                             if (resolvedSensorObj == null) resolvedSensorObj = GameObject.Find(sensorName);
 
                                             GameObject prodGo = null;
                                             var dynamicProduct = FindFirstObjectByType<AAS.DynamicProduct>();
                                             if (dynamicProduct != null)
                                             {
                                                 prodGo = dynamicProduct.gameObject;
                                             }
 
                                             if (prodGo != null && resolvedSensorObj != null && sensorCtrl.HasSnapConfig(sensorName))
                                             {
                                                 Rigidbody rb = prodGo.GetComponentInParent<Rigidbody>();
                                                 if (rb != null)
                                                 {
                                                     rb.position = new Vector3(resolvedSensorObj.transform.position.x, rb.position.y, resolvedSensorObj.transform.position.z);
                                                     rb.rotation = Quaternion.Euler(0, resolvedSensorObj.transform.eulerAngles.y, 0);
                                                     
                                                     bool wasKinematic = rb.isKinematic;
                                                     rb.isKinematic = true;
                                                     rb.isKinematic = wasKinematic;
                                                     rb.linearVelocity = Vector3.zero;
                                                     rb.angularVelocity = Vector3.zero;
                                                     Physics.SyncTransforms();
                                                     
                                                     Debug.Log($"[RuleEngine] ⚡ DT Sync Snap: Teleported '{prodGo.name}' to '{sensorName}' because real sensor triggered.");
                                                 }
                                             }
                                         }
                                     }
                                 }
                                 break;
                             }
                         }
                         else
                         {
                             if (condSim)
                             {
                                 break;
                             }
                         }
 
                         yield return new WaitForSeconds(0.1f);
                     }
                     break;
                 }
                case "if":
                case "if_else":
                {
                    JToken cond = step["condition"];
                    bool isTrue = EvaluateCondition(cond);

                    if (isTrue)
                    {
                        JArray thenBlock = step["then"] as JArray;
                        if (thenBlock != null) yield return StartCoroutine(ExecuteBlockRoutine(seqName, thenBlock));
                    }
                    else
                    {
                        JArray elseBlock = step["else"] as JArray;
                        if (elseBlock != null) yield return StartCoroutine(ExecuteBlockRoutine(seqName, elseBlock));
                    }
                    break;
                }

                case "while":
                case "repeat_while":
                {
                    JToken cond = step["condition"];
                    JArray body = step["body"] as JArray ?? step["then"] as JArray;
                    while (EvaluateCondition(cond))
                    {
                        if (body != null) yield return StartCoroutine(ExecuteBlockRoutine(seqName, body));
                        yield return null;
                    }
                    break;
                }

                case "repeat_times":
                {
                    int times = step["times"]?.ToObject<int>() ?? 0;
                    JArray body = step["body"] as JArray ?? step["then"] as JArray;
                    for (int i = 0; i < times; i++)
                    {
                        if (body != null) yield return StartCoroutine(ExecuteBlockRoutine(seqName, body));
                        yield return null;
                    }
                    break;
                }

                case "repeat_forever":
                {
                    JArray body = step["body"] as JArray ?? step["then"] as JArray;
                    while (true)
                    {
                        if (body != null) yield return StartCoroutine(ExecuteBlockRoutine(seqName, body));
                        yield return null;
                    }
                }

                case "set_variable":
                {
                    string varName = step["variable"]?.ToString();
                    JToken valToken = step["value"];
                    if (!string.IsNullOrEmpty(varName) && valToken != null)
                    {
                        variables[varName] = valToken.ToObject<object>();
                        Debug.Log($"[RuleEngine] [{seqName}] Set Var '{varName}' = {variables[varName]}");
                    }
                    break;
                }

                case "call_http_service":
                {
                    if (dtMode)
                    {
                        string url = step["url"]?.ToString();
                        string method = step["method"]?.ToString() ?? "POST";
                        string payload = step["payload"]?.ToString() ?? "";
                        StartCoroutine(SendHttpRequestRoutine(seqName, url, method, payload));
                    }
                    else
                    {
                        Debug.Log($"[RuleEngine] [{seqName}] ℹ️ Skipping HTTP call step in VC mode (DT mode only): {step["url"]}");
                    }
                    break;
                }

                default:
                    Debug.LogWarning($"[RuleEngine] [{seqName}] Unknown step type: '{type}'");
                    break;
            }
        }
    }

    private IEnumerator SendHttpRequestRoutine(string seqName, string url, string method, string payload)
    {
        if (string.IsNullOrEmpty(url))
        {
            Debug.LogWarning($"[RuleEngine] [{seqName}] ⚠️ HTTP call url is empty.");
            yield break;
        }

        if (!url.StartsWith("http://") && !url.StartsWith("https://"))
        {
            url = "http://" + url;
        }

        int maxRetries = 5;
        int attempt = 0;
        bool success = false;

        while (attempt < maxRetries && !success)
        {
            attempt++;
            Debug.Log($"[RuleEngine] [{seqName}] 📞 Sending HTTP {method} request to {url} (Attempt {attempt}/{maxRetries})");
            
            using (UnityWebRequest req = new UnityWebRequest(url, method))
            {
                if (!string.IsNullOrEmpty(payload) && (method == "POST" || method == "PUT"))
                {
                    byte[] bodyRaw = System.Text.Encoding.UTF8.GetBytes(payload);
                    req.uploadHandler = new UploadHandlerRaw(bodyRaw);
                    req.downloadHandler = new DownloadHandlerBuffer();
                    req.SetRequestHeader("Content-Type", "application/json");
                }
                else
                {
                    req.downloadHandler = new DownloadHandlerBuffer();
                }

                req.timeout = 60;
                yield return req.SendWebRequest();

                if (req.result == UnityWebRequest.Result.Success)
                {
                    Debug.Log($"[RuleEngine] [{seqName}] ✅ HTTP {method} to {url} Success: {req.downloadHandler.text}");
                    success = true;
                }
                else
                {
                    Debug.LogWarning($"[RuleEngine] [{seqName}] ❌ HTTP {method} to {url} Failed (Attempt {attempt}): {req.error}");
                    if (attempt < maxRetries)
                    {
                        Debug.Log($"[RuleEngine] [{seqName}] 🕒 Waiting 1 second before retrying...");
                        yield return new WaitForSeconds(1.0f);
                    }
                }
            }
        }

        if (!success)
        {
            Debug.LogError($"[RuleEngine] [{seqName}] ❌ HTTP {method} to {url} failed completely after {maxRetries} attempts.");
        }
    }

    private void ShowWarningUI(string message)
    {
        if (warningPanel != null)
        {
            warningPanel.SetActive(true);
        }
        else
        {
            // Fallback: try to find or create a warning display on the runtime Canvas
            var canvasGo = GameObject.Find("ModeOverlayCanvas");
            if (canvasGo != null)
            {
                var dynPanel = canvasGo.transform.Find("DynamicWarningPanel")?.gameObject;
                if (dynPanel == null)
                {
                    dynPanel = new GameObject("DynamicWarningPanel");
                    dynPanel.transform.SetParent(canvasGo.transform, false);
                    var rect = dynPanel.AddComponent<RectTransform>();
                    rect.anchorMin = new Vector2(0f, 0f);
                    rect.anchorMax = new Vector2(1f, 0.15f);
                    rect.offsetMin = Vector2.zero;
                    rect.offsetMax = Vector2.zero;
                    var img = dynPanel.AddComponent<Image>();
                    img.color = new Color(0.7f, 0.13f, 0.13f, 0.95f); // Red alert

                    var textGo = new GameObject("WarningText");
                    textGo.transform.SetParent(dynPanel.transform, false);
                    var tRect = textGo.AddComponent<RectTransform>();
                    tRect.anchorMin = Vector2.zero;
                    tRect.anchorMax = Vector2.one;
                    tRect.offsetMin = new Vector2(20, 20);
                    tRect.offsetMax = new Vector2(-20, -20);
                    var txt = textGo.AddComponent<Text>();
                    txt.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
                    txt.fontSize = 24;
                    txt.fontStyle = FontStyle.Bold;
                    txt.alignment = TextAnchor.MiddleCenter;
                    txt.color = Color.white;
                }
                dynPanel.SetActive(true);
                var dynTxt = dynPanel.GetComponentInChildren<Text>();
                if (dynTxt != null) dynTxt.text = message;
            }
        }

        if (warningTextTMP != null)
        {
            warningTextTMP.text = message;
        }
    }

    private void HideWarningUI()
    {
        if (warningPanel != null)
        {
            warningPanel.SetActive(false);
        }
        else
        {
            var canvasGo = GameObject.Find("ModeOverlayCanvas");
            if (canvasGo != null)
            {
                var dynPanel = canvasGo.transform.Find("DynamicWarningPanel")?.gameObject;
                if (dynPanel != null)
                {
                    dynPanel.SetActive(false);
                }
            }
        }

        if (warningTextTMP != null)
        {
            warningTextTMP.text = "";
        }
    }

    private bool EvaluateCondition(JToken condToken, bool forceRealAAS = false, bool useRealState = false)
    {
        if (condToken == null) return false;

        if (condToken is JObject obj)
        {
            // Support legacy multi-condition container with "operator" and "conditions"
            if (obj["conditions"] is JArray condsArray)
            {
                string op = obj["operator"]?.ToString() ?? "AND";
                bool andResult = true;
                bool orResult = false;

                foreach (JToken item in condsArray)
                {
                    bool itemRes = EvaluateCondition(item, forceRealAAS, useRealState);
                    andResult &= itemRes;
                    orResult |= itemRes;
                }
                return op.Equals("OR", StringComparison.OrdinalIgnoreCase) ? orResult : andResult;
            }

            // Sequence active check
            if (obj["sequence"] != null)
            {
                string seqName = obj["sequence"]?.ToString();
                bool targetVal = obj["value"]?.ToObject<bool>() ?? true;
                bool actualVal = activeSequences.Contains(seqName);
                return actualVal == targetVal;
            }

            // Single sensor check (legacy or new leaf)
            if (obj["sensor"] != null)
            {
                string sensor = obj["sensor"]?.ToString();
                if (!string.IsNullOrEmpty(sensor) && sensor.StartsWith("[Rule] "))
                {
                    string seqNameLegacy = sensor.Substring(7).Trim();
                    bool targetValLegacy = obj["value"]?.ToObject<bool>() ?? true;
                    bool actualValLegacy = activeSequences.Contains(seqNameLegacy);
                    return actualValLegacy == targetValLegacy;
                }

                string kit = obj["kit"]?.ToString();
                if (string.IsNullOrEmpty(kit)) kit = GetLocalKitName();

                bool targetValSensor = obj["value"]?.ToObject<bool>() ?? true;
                bool actualVal = forceRealAAS ? driver.ReadSensorFromAAS(kit, sensor) : driver.ReadSensor(kit, sensor, useRealState);

                string compOp = obj["operator"]?.ToString() ?? "==";
                if (compOp == "!=") return actualVal != targetValSensor;
                return actualVal == targetValSensor;
            }

            // Variable comparison check
            if (obj["variable"] != null)
            {
                string varName = obj["variable"]?.ToString();
                JToken targetValToken = obj["value"];
                if (variables.TryGetValue(varName, out object currentVal) && targetValToken != null)
                {
                    string compOp = obj["operator"]?.ToString() ?? "==";
                    string s1 = currentVal?.ToString();
                    string s2 = targetValToken.ToString();

                    if (double.TryParse(s1, out double d1) && double.TryParse(s2, out double d2))
                    {
                        switch (compOp)
                        {
                            case ">": return d1 > d2;
                            case "<": return d1 < d2;
                            case ">=": return d1 >= d2;
                            case "<=": return d1 <= d2;
                            case "!=": return Math.Abs(d1 - d2) > 0.0001;
                            default: return Math.Abs(d1 - d2) < 0.0001;
                        }
                    }
                    return compOp == "!=" ? s1 != s2 : s1 == s2;
                }
                return false;
            }
        }

        return false;
    }

    public JToken GetSequenceToken(string seqName)
    {
        if (sequences == null) return null;
        foreach (var seq in sequences)
        {
            if (seq["name"]?.ToString() == seqName)
            {
                return seq;
            }
        }
        return null;
    }

    public string GetLocalKitName()
    {
        string name = gameObject.name;
        if (name.EndsWith("(Clone)"))
        {
            name = name.Substring(0, name.Length - 7).Trim();
        }
        return name;
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
