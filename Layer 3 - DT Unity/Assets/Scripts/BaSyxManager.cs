using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Linq;
using System.Xml.Linq;
using UnityEngine;
using UnityEngine.UI;
using UnityEngine.Networking;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Unity.Robotics.UrdfImporter;
using UnityEngine.SceneManagement;
using TMPro;

public class BaSyxManager : MonoBehaviour
{
    public static bool IsStartupComplete { get; private set; } = false;

    [Header("BaSyx Infrastructure")]
    public string registryUrl = "http://localhost:8082/shell-descriptors";
    public string aasServerUrl = "http://localhost:8081";

    [Header("Error / Warning UI (Passed to Rule Engines)")]
    public GameObject warningPanel;
    public TextMeshProUGUI warningTextTMP;
    public TextMeshProUGUI startupTimeTMP;

    [Header("Settings")]
    public float globalScale = 1.0f;
    public float discoveryInterval = 2f;
    public float positionSyncInterval = 0.1f;
    public float startupStabilizationDelay = 10.0f;
    public string baseCachePath;

    private Dictionary<string, GameObject> instantiatedRobots = new Dictionary<string, GameObject>();
    private HashSet<string> pendingRobotIds = new HashSet<string>();
    private Dictionary<string, bool?> pendingRobotDynamics = new Dictionary<string, bool?>();
    private Dictionary<string, string> kitIdToPinsSubmodelId = new Dictionary<string, string>();
    private bool readyToSpawnDynamics = false;

    private class PendingRuleEngineInit
    {
        public AASRuleEngine engine;
        public string controlLogicSubmodelId;
        public AASHttpDriver driver;
        public string skillServerURL;
    }
    private List<PendingRuleEngineInit> pendingRules = new List<PendingRuleEngineInit>();

    public string GetVirtualPinsSubmodelId(string kitIdShort)
    {
        if (kitIdToPinsSubmodelId.TryGetValue(kitIdShort, out string smId)) return smId;
        return "https://acplt.org/Submodels/Pins_" + kitIdShort; // fallback standard pattern
    }

    public class MaterialData
    {
        public Color color = Color.white;
        public string texturePath = null;
        public bool hasColor = false;
        public bool hasTexture = false;
    }

    private System.Diagnostics.Stopwatch startupStopwatch;
    private float startupRealtimeSinceStartup;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
    private static void OnBeforeSceneLoad()
    {
        Debug.Log($"[StartupTimer] ⏱️ Engine Startup / BeforeSceneLoad iniciado aos {Time.realtimeSinceStartupAsDouble:F4}s");
    }

    void Awake()
    {
        startupStopwatch = System.Diagnostics.Stopwatch.StartNew();
        startupRealtimeSinceStartup = Time.realtimeSinceStartup;
        Debug.Log($"[StartupTimer] ⏱️ BaSyxManager.Awake iniciado aos {startupRealtimeSinceStartup:F4}s desde o arranque do Unity!");

        baseCachePath = Path.Combine(Application.persistentDataPath, "AAS_Robots");
    }

    void Start()
    {
        Debug.Log("[BaSyx] 🧹 Cleaning local cache...");
        if (Directory.Exists(baseCachePath)) Directory.Delete(baseCachePath, true);
        Directory.CreateDirectory(baseCachePath);

        StartCoroutine(OrchestratedStartupRoutine());
    }

    void Update()
    {
        bool rPressed = false;
#if ENABLE_INPUT_SYSTEM
        if (UnityEngine.InputSystem.Keyboard.current != null && UnityEngine.InputSystem.Keyboard.current.rKey.wasPressedThisFrame)
            rPressed = true;
#endif
#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        if (Input.GetKeyDown(KeyCode.R))
            rPressed = true;
#endif
        if (rPressed)
        {
            Debug.Log("[BaSyx] 🔄 [R Key Pressed] Reiniciando cena atual...");
            SceneManager.LoadScene(SceneManager.GetActiveScene().buildIndex);
        }
    }

    IEnumerator OrchestratedStartupRoutine()
    {
        // 0. Pre-flight Check: Ensure HTTP services (Registry & AAS Server) are reachable
        Debug.Log("[BaSyx] 🔍 Verificando conectividade HTTP com Registry e AAS Server...");
        bool httpConnected = false;
        while (!httpConnected)
        {
            using (UnityWebRequest testReg = UnityWebRequest.Get(registryUrl))
            {
                testReg.timeout = 3;
                yield return testReg.SendWebRequest();

                if (testReg.result == UnityWebRequest.Result.Success)
                {
                    httpConnected = true;
                }
                else
                {
                    Debug.LogError($"[BaSyx] ⛔ Não é possível conectar ao Registry HTTP ({registryUrl}): {testReg.error}. A aguardar conectividade antes de iniciar...");
                    yield return new WaitForSeconds(2.0f);
                }
            }
        }
        Debug.Log("[BaSyx] 🌐 Conectividade HTTP verificada com sucesso.");

        // 1. Run initial synchronization to discover and start loading static shells
        yield return StartCoroutine(SyncRobotsRoutine());
        
        // 2. Wait until all discovered static kits (non-dynamic) finish loading and spawning
        while (true)
        {
            bool anyStaticPending = false;
            foreach (var kvp in pendingRobotDynamics)
            {
                if (kvp.Value == false || kvp.Value == null)
                {
                    if (pendingRobotIds.Contains(kvp.Key))
                    {
                        anyStaticPending = true;
                        break;
                    }
                }
            }
            if (!anyStaticPending) break;
            yield return new WaitForSeconds(0.2f);
        }
        
        // 3. Wait for the designated stabilization delay
        Debug.Log($"[BaSyx] ⏳ Waiting {startupStabilizationDelay}s for system physics/outputs to stabilize...");
        yield return new WaitForSeconds(startupStabilizationDelay);
        
        // 4. Trigger readyToSpawnDynamics flag so the waiting dynamic kits can now build
        readyToSpawnDynamics = true;
        
        // 5. Wait for all dynamic kits to finish spawning
        while (pendingRobotIds.Count > 0)
        {
            yield return new WaitForSeconds(0.2f);
        }
        
        // 6. Initialize all accumulated rule engines in sequence (only in Virtual Commissioning)
        if (OperationModeManager.Current == OperationMode.VirtualCommissioning)
        {
            Debug.Log("[BaSyx] 🚀 Initializing all AAS Rule Engines now!");
            foreach (var p in pendingRules)
            {
                if (p.engine != null)
                {
                    p.engine.warningPanel = warningPanel;
                    p.engine.warningTextTMP = warningTextTMP;
                    p.engine.Init(aasServerUrl, p.controlLogicSubmodelId, p.driver, p.skillServerURL);
                }
            }
            pendingRules.Clear();
        }
        else
        {
            Debug.Log("[BaSyx] ⏸️ Digital Twin mode active: skipping AAS Rule Engines initialization at startup.");
        }
        
        if (AASArduinoLink.Instance != null)
        {
            AASArduinoLink.Instance.ConnectAll();
        }

        if (OperationModeManager.Current == OperationMode.DigitalTwin)
        {
            if (AASArduinoLink.Instance != null && AASArduinoLink.Instance.RegisteredEndpointCount > 0)
            {
                Debug.Log("[BaSyx] 🔌 Modo Digital Twin ativo: A aguardar conexão por Socket/TCP aos endpoints de hardware...");
                while (!AASArduinoLink.Instance.HasActiveConnections())
                {
                    Debug.LogWarning("[BaSyx] ⚠️ DT Socket não conectado ao hardware! O DT não iniciará até estabelecer conexão...");
                    yield return new WaitForSeconds(2.0f);
                }
                Debug.Log("[BaSyx] ⚡ Conexão Socket ao hardware estabelecida com sucesso!");
            }

            // Wait 4 seconds for snaps and initial sensor synchronization to finish
            yield return new WaitForSeconds(4.0f);
            
            Debug.Log("[BaSyx] 🚀 Startup delay over: Initializing AAS Rule Engines in Digital Twin mode.");
            foreach (var p in pendingRules)
            {
                if (p.engine != null)
                {
                    p.engine.warningPanel = warningPanel;
                    p.engine.warningTextTMP = warningTextTMP;
                    p.engine.Init(aasServerUrl, p.controlLogicSubmodelId, p.driver, p.skillServerURL);
                }
            }
            pendingRules.Clear();
        }

        IsStartupComplete = true;
        if (startupStopwatch != null)
        {
            startupStopwatch.Stop();
            float totalSecondsAwake = (float)startupStopwatch.Elapsed.TotalSeconds;
            float totalSecondsEngine = Time.realtimeSinceStartup;
            string timingLog = $"<color=cyan><b>[StartupTimer] 🏁 INICIALIZAÇÃO COMPLETA!</b></color>\n" +
                               $"⏱️ Tempo desde Awake: <b>{totalSecondsAwake:F3}s</b> ({startupStopwatch.ElapsedMilliseconds} ms)\n" +
                               $"⏱️ Tempo total desde arranque do Unity: <b>{totalSecondsEngine:F3}s</b>\n" +
                               $"📦 Kits instanciados: {instantiatedRobots.Count}";
            Debug.Log(timingLog);
        }
        Debug.Log("[BaSyx] 🎉 Orchestrated Startup is fully complete! Systems are ready.");
        
        // 7. Hand over control to the standard DiscoveryLoop for runtime AAS addition/removal
        StartCoroutine(DiscoveryLoop());
    }

    IEnumerator DiscoveryLoop()
    {
        while (true)
        {
            yield return StartCoroutine(SyncRobotsRoutine());
            yield return new WaitForSeconds(discoveryInterval);
        }
    }

    IEnumerator SyncRobotsRoutine()
    {
        // 1. Get AAS Descriptors from Discovery/Registry
        UnityWebRequest regReq = UnityWebRequest.Get(registryUrl);
        yield return regReq.SendWebRequest();

        if (regReq.result != UnityWebRequest.Result.Success)
        {
            Debug.LogError($"[BaSyx] ❌ Registry error: {regReq.error}");
            yield break;
        }

        var shells = DeserializeList<AasDescriptor>(regReq.downloadHandler.text);
        HashSet<string> currentRegistryIds = new HashSet<string>(shells.Select(s => s.id));

        // 1. Handle Removals
        var idsToRemove = instantiatedRobots.Keys.Where(id => !currentRegistryIds.Contains(id)).ToList();
        foreach (var id in idsToRemove)
        {
            Debug.Log($"[BaSyx] 🗑️ Removing robot: {id}");
            if (instantiatedRobots[id] != null)
            {
                if (RobotCameraManager.Instance != null) RobotCameraManager.Instance.RemoveTarget(instantiatedRobots[id].transform);
                Destroy(instantiatedRobots[id]);
            }
            instantiatedRobots.Remove(id);
        }
        
        // 2. Handle Additions
        foreach (var shell in shells)
        {
            if (instantiatedRobots.ContainsKey(shell.id) || pendingRobotIds.Contains(shell.id)) continue;
            
            pendingRobotIds.Add(shell.id);
            pendingRobotDynamics[shell.id] = null;
            StartCoroutine(ProcessRobot(shell));
        }
    }

    IEnumerator ProcessRobot(AasDescriptor shell)
    {
        string robotName = string.IsNullOrEmpty(shell.idShort) ? "UnknownRobot" : shell.idShort;
        string robotPath = Path.Combine(baseCachePath, robotName);

        try
        {
            try
            {
                Directory.CreateDirectory(robotPath);
            }
            catch (Exception e)
            {
                Debug.LogError($"[BaSyx] Failed to create directory {robotPath}: {e.Message}");
            }

            if ((shell.submodelDescriptors == null || shell.submodelDescriptors.Count == 0) && !string.IsNullOrEmpty(shell.id))
            {
                string detailUrl = $"{registryUrl}/{Base64Url(shell.id)}";
                UnityWebRequest detReq = UnityWebRequest.Get(detailUrl);
                yield return detReq.SendWebRequest();
                if (detReq.result == UnityWebRequest.Result.Success)
                {
                    try
                    {
                        var detailed = JsonConvert.DeserializeObject<AasDescriptor>(detReq.downloadHandler.text);
                        if (detailed != null && detailed.submodelDescriptors != null)
                            shell.submodelDescriptors = detailed.submodelDescriptors;
                    }
                    catch (Exception e)
                    {
                        Debug.LogError($"[BaSyx] JSON Deserialization error for detailed shell: {e.Message}");
                    }
                }
            }

            string geoSubmodelId = null;
            string posSubmodelId = null;
            string telSubmodelId = null;
            string visSubmodelId = null;
            string pinSubmodelId = null;
            string behSubmodelId = null;
            string controlLogicSubmodelId = null;
            string aidSubmodelId = null;

            if (shell.submodelDescriptors != null)
            {
                foreach (var sm in shell.submodelDescriptors)
                {
                    if (sm == null) continue;
                    if (sm.idShort == "GeometryData") geoSubmodelId = sm.id;
                    if (sm.idShort == "PositionData") posSubmodelId = sm.id;
                    if (sm.idShort == "TelemetryData") telSubmodelId = sm.id;
                    if (sm.idShort == "VisualStructure") visSubmodelId = sm.id;
                    if (sm.idShort == "VirtualPins") pinSubmodelId = sm.id;
                    if (sm.idShort == "BehaviorMapping") behSubmodelId = sm.id;
                    if (sm.idShort == "ControlLogic") controlLogicSubmodelId = sm.id;
                    if (sm.idShort == "AssetInterfacesDescription") aidSubmodelId = sm.id;
                }
            }

            if ((geoSubmodelId == null || telSubmodelId == null) && visSubmodelId == null && !string.IsNullOrEmpty(shell.id))
            {
                string repoShellUrl = $"{aasServerUrl}/shells/{Base64Url(shell.id)}";
                UnityWebRequest repoReq = UnityWebRequest.Get(repoShellUrl);
                yield return repoReq.SendWebRequest();

                if (repoReq.result == UnityWebRequest.Result.Success)
                {
                    AasRepositoryModel repoAas = null;
                    try
                    {
                        repoAas = JsonConvert.DeserializeObject<AasRepositoryModel>(repoReq.downloadHandler.text);
                    }
                    catch (Exception e)
                    {
                        Debug.LogError($"[BaSyx] JSON Deserialization error for repo shell: {e.Message}");
                    }

                    if (repoAas != null && repoAas.submodels != null)
                    {
                        foreach (var smRef in repoAas.submodels)
                        {
                            if (smRef.keys == null || smRef.keys.Count == 0) continue;
                            string smId = smRef.keys[0].value;
                            string smMetaUrl = $"{aasServerUrl}/submodels/{Base64Url(smId)}";
                            UnityWebRequest smReq = UnityWebRequest.Get(smMetaUrl);
                            yield return smReq.SendWebRequest();

                            if (smReq.result == UnityWebRequest.Result.Success)
                            {
                                SubmodelDescriptor smDesc = null;
                                try
                                {
                                    smDesc = JsonConvert.DeserializeObject<SubmodelDescriptor>(smReq.downloadHandler.text);
                                }
                                catch (Exception e)
                                {
                                    Debug.LogError($"[BaSyx] JSON Deserialization error for submodel: {e.Message}");
                                }

                                if (smDesc != null)
                                {
                                    if (smDesc.idShort == "GeometryData") geoSubmodelId = smDesc.id;
                                    if (smDesc.idShort == "PositionData") posSubmodelId = smDesc.id;
                                    if (smDesc.idShort == "TelemetryData") telSubmodelId = smDesc.id;
                                    if (smDesc.idShort == "VisualStructure") visSubmodelId = smDesc.id;
                                    if (smDesc.idShort == "VirtualPins") pinSubmodelId = smDesc.id;
                                    if (smDesc.idShort == "BehaviorMapping") behSubmodelId = smDesc.id;
                                    if (smDesc.idShort == "ControlLogic") controlLogicSubmodelId = smDesc.id;
                                    if (smDesc.idShort == "AssetInterfacesDescription") aidSubmodelId = smDesc.id;
                                }
                            }
                        }
                    }
                }
            }

            if (pinSubmodelId != null)
            {
                if (!string.IsNullOrEmpty(shell.idShort))
                {
                    kitIdToPinsSubmodelId[shell.idShort] = pinSubmodelId;
                }
                // Clear all actuators to zero immediately on discovery before spawning objects
                yield return StartCoroutine(ResetVirtualPinsRoutine(pinSubmodelId));
            }

            if (geoSubmodelId == null && visSubmodelId == null && controlLogicSubmodelId != null)
            {
                GameObject logicalGo = new GameObject("LogicalAAS_" + shell.idShort);
                GameObject container = GameObject.Find("AAS_Kits_Container");
                if (container == null) container = new GameObject("AAS_Kits_Container");
                logicalGo.transform.SetParent(container.transform, false);

                var engine = logicalGo.AddComponent<AASRuleEngine>();
                var driver = new AASHttpDriver(null, null, aasServerUrl, GetVirtualPinsSubmodelId(shell.idShort));
                pendingRules.Add(new PendingRuleEngineInit {
                    engine = engine,
                    controlLogicSubmodelId = controlLogicSubmodelId,
                    driver = driver,
                    skillServerURL = ""
                });
                Debug.Log($"[BaSyx] Staged Purely Logical AAS (no GLB): '{shell.idShort}' for startup sequence initialization.");

                instantiatedRobots[shell.id] = logicalGo;
                pendingRobotIds.Remove(shell.id);
                pendingRobotDynamics.Remove(shell.id);
                yield break;
            }

            if (geoSubmodelId == null && visSubmodelId == null) 
            { 
                pendingRobotIds.Remove(shell.id); 
                pendingRobotDynamics.Remove(shell.id); 
                yield break; 
            }

            // --- DETERMINE DYNAMIC PROPERTY ---
            bool isDynamic = false;
            bool canHaveMultiple = false;
            string elementsJson = null;

            if (visSubmodelId != null)
            {
                string elementsUrl = $"{aasServerUrl}/submodels/{Base64Url(visSubmodelId)}/submodel-elements?level=deep";
                UnityWebRequest req = UnityWebRequest.Get(elementsUrl);
                yield return req.SendWebRequest();
                
                if (req.result == UnityWebRequest.Result.Success)
                {
                    elementsJson = req.downloadHandler.text;
                    isDynamic = CheckIsDynamicFromJson(elementsJson);
                    canHaveMultiple = CheckCanHaveMultipleFromJson(elementsJson);
                }
            }
            
            // --- READ ASSET INTERFACES DESCRIPTION (AID) ---
            string kitProtocol = "tcp";
            string kitEndpointIP = "";
            int kitEndpointPort = 8888;
            string skillServerURL = "";

            if (aidSubmodelId != null)
            {
                string aidUrl = $"{aasServerUrl}/submodels/{Base64Url(aidSubmodelId)}/submodel-elements?level=deep";
                UnityWebRequest req = UnityWebRequest.Get(aidUrl);
                yield return req.SendWebRequest();
                if (req.result == UnityWebRequest.Result.Success)
                {
                    try
                    {
                        var elements = DeserializeList<SubmodelElement>(req.downloadHandler.text);
                        foreach (var el in elements)
                        {
                            if (el.idShort == "Protocol" && el.value != null) kitProtocol = el.value.ToString();
                            if (el.idShort == "EndpointIP" && el.value != null) kitEndpointIP = el.value.ToString();
                            if (el.idShort == "EndpointPort" && el.value != null)
                            {
                                int.TryParse(el.value.ToString(), out kitEndpointPort);
                            }
                        }
                        if (!string.IsNullOrEmpty(kitEndpointIP))
                        {
                            skillServerURL = $"http://{kitEndpointIP}:80";
                        }
                    }
                    catch (Exception ex)
                    {
                        Debug.LogError($"[BaSyx] Error parsing AID submodel: {ex.Message}");
                    }
                }
            }

            pendingRobotDynamics[shell.id] = isDynamic;

            // If it is dynamic, wait for the global readyToSpawnDynamics trigger
            if (isDynamic)
            {
                Debug.Log($"[BaSyx] ⏳ Kit '{robotName}' is DYNAMIC. Waiting for system stabilization sequence to complete...");
                yield return new WaitUntil(() => readyToSpawnDynamics);
                Debug.Log($"[BaSyx] 🚀 Spawning dynamic kit '{robotName}' now.");
            }

            // 1. PRIORITIZE URDF (GeometryData)
            if (geoSubmodelId != null)
            {
                Debug.Log($"[BaSyx] 📥 Downloading assets for {robotName} (URDF)...");
                yield return StartCoroutine(DownloadRecursive(geoSubmodelId, robotPath));

                string urdfFile = FindUrdfFile(robotPath);
                if (urdfFile != null)
                {
                    AASUrdfImporter.ImportResult importResult = null;
                    try
                    {
                        importResult = AASUrdfImporter.Import(urdfFile, robotName);
                    }
                    catch (Exception e)
                    {
                        Debug.LogError($"[BaSyx] Error importing URDF: {e.Message}");
                    }

                    if (importResult != null && importResult.root != null)
                    {
                        GameObject robotObj = importResult.root;
                        try
                        {
                            PrepareRobot(robotObj, urdfFile);
                        }
                        catch (Exception e)
                        {
                            Debug.LogError($"[BaSyx] Error preparing URDF robot: {e.Message}");
                        }

                        GameObject container = GameObject.Find("AAS_Kits_Container");
                        if (container == null) container = new GameObject("AAS_Kits_Container");
                        robotObj.transform.SetParent(container.transform, false);
                        robotObj.transform.localScale = Vector3.one * globalScale;

                        if (isDynamic)
                        {
                            var prodConfig = robotObj.AddComponent<AASProductConfig>();
                            prodConfig.canHaveMultiple = canHaveMultiple;
                        }

                        var jointInitList = new List<AASRobotSync.JointInitData>();
                        foreach (var jData in importResult.joints)
                        {
                            jointInitList.Add(new AASRobotSync.JointInitData {
                                name = jData.name,
                                transform = jData.anchor.transform,
                                axis = jData.axis
                            });
                        }

                        instantiatedRobots[shell.id] = robotObj;
                        
                        if (RobotCameraManager.Instance != null) RobotCameraManager.Instance.AddTarget(robotObj.transform);
                        if (posSubmodelId != null) StartCoroutine(SyncPositionLoop(posSubmodelId, robotObj));
                        
                        // Joint Telemetry Sync
                        if (telSubmodelId != null)
                        {
                            try
                            {
                                var sync = robotObj.AddComponent<AASRobotSync>();
                                sync.SetJointMap(jointInitList);
                                sync.Init(telSubmodelId, aasServerUrl);
                            }
                            catch (Exception e)
                            {
                                Debug.LogError($"[BaSyx] Error adding AASRobotSync: {e.Message}");
                            }
                        }

                        // Virtual IO Sync (for Kits)
                        if (pinSubmodelId != null && behSubmodelId != null)
                        {
                            AASActuatorController actuatorCtrl = null;
                            AASSensorController sensorCtrl = null;
                            bool ioInitSuccess = false;

                            try
                            {
                                var components = new Dictionary<string, GameObject>();
                                foreach (Transform t in robotObj.GetComponentsInChildren<Transform>()) components[t.name] = t.gameObject;

                                actuatorCtrl = robotObj.AddComponent<AASActuatorController>();
                                actuatorCtrl.Init(aasServerUrl, pinSubmodelId, behSubmodelId, components, kitProtocol, kitEndpointIP, kitEndpointPort);

                                sensorCtrl = robotObj.AddComponent<AASSensorController>();
                                sensorCtrl.Init(aasServerUrl, pinSubmodelId, behSubmodelId, components, kitProtocol, kitEndpointIP, kitEndpointPort);

                                ioInitSuccess = true;
                            }
                            catch (Exception e)
                            {
                                Debug.LogError($"[BaSyx] Error initializing URDF actuator/sensor controllers: {e.Message}");
                            }

                            if (ioInitSuccess && controlLogicSubmodelId != null)
                            {
                                var engine = robotObj.AddComponent<AASRuleEngine>();
                                var driver = new AASHttpDriver(sensorCtrl, actuatorCtrl, aasServerUrl, pinSubmodelId);
                                pendingRules.Add(new PendingRuleEngineInit {
                                    engine = engine,
                                    controlLogicSubmodelId = controlLogicSubmodelId,
                                    driver = driver,
                                    skillServerURL = skillServerURL
                                });
                                Debug.Log($"[BaSyx] Staged Rule Engine for URDF kit '{robotName}' for startup sequence initialization.");
                            }
                        }

                        Debug.Log($"[BaSyx] ✅ Robot/Kit {robotName} prepared via URDF.");
                        pendingRobotIds.Remove(shell.id);
                        pendingRobotDynamics.Remove(shell.id);
                        yield break;
                    }
                }
            }

            // 2. FALLBACK to Kit (VisualStructure)
            if (visSubmodelId != null)
            {
                Debug.Log($"[BaSyx] 📥 Downloading assets for Kit {robotName} (VisualStructure)...");
                yield return StartCoroutine(DownloadRecursive(visSubmodelId, robotPath));
                
                if (!string.IsNullOrEmpty(elementsJson))
                {
                    // Async GLB Import
                    var task = AASKitImporter.ImportWithMetadataAsync(elementsJson, robotPath, robotName);
                    yield return new WaitUntil(() => task.IsCompleted);
                    
                    AASKitImporter.KitImportData importData = null;
                    try
                    {
                        importData = task.Result;
                    }
                    catch (Exception e)
                    {
                        Debug.LogError($"[BaSyx] Error importing GLB kit: {e.Message}");
                    }
                    
                    if (importData != null && importData.root != null)
                    {
                        GameObject kitObj = importData.root;
                        GameObject container = GameObject.Find("AAS_Kits_Container");
                        if (container == null) container = new GameObject("AAS_Kits_Container");
                        kitObj.transform.SetParent(container.transform, false);
                        kitObj.transform.localScale = Vector3.one * globalScale;

                        if (isDynamic)
                        {
                            var prodConfig = kitObj.AddComponent<AASProductConfig>();
                            prodConfig.canHaveMultiple = canHaveMultiple;
                        }

                        instantiatedRobots[shell.id] = kitObj;
                        if (RobotCameraManager.Instance != null) RobotCameraManager.Instance.AddTarget(kitObj.transform);
                        
                        if (pinSubmodelId != null && behSubmodelId != null)
                        {
                            AASActuatorController actuatorCtrl = null;
                            AASSensorController sensorCtrl = null;
                            bool ioInitSuccess = false;

                            try
                            {
                                actuatorCtrl = kitObj.AddComponent<AASActuatorController>();
                                actuatorCtrl.Init(aasServerUrl, pinSubmodelId, behSubmodelId, importData.components, kitProtocol, kitEndpointIP, kitEndpointPort);

                                sensorCtrl = kitObj.AddComponent<AASSensorController>();
                                sensorCtrl.Init(aasServerUrl, pinSubmodelId, behSubmodelId, importData.components, kitProtocol, kitEndpointIP, kitEndpointPort, startupStabilizationDelay);

                                ioInitSuccess = true;
                            }
                            catch (Exception e)
                            {
                                Debug.LogError($"[BaSyx] Error initializing GLB actuator/sensor controllers: {e.Message}");
                            }

                            if (ioInitSuccess && controlLogicSubmodelId != null)
                            {
                                var engine = kitObj.AddComponent<AASRuleEngine>();
                                var driver = new AASHttpDriver(sensorCtrl, actuatorCtrl, aasServerUrl, pinSubmodelId);
                                pendingRules.Add(new PendingRuleEngineInit {
                                    engine = engine,
                                    controlLogicSubmodelId = controlLogicSubmodelId,
                                    driver = driver,
                                    skillServerURL = skillServerURL
                                });
                                Debug.Log($"[BaSyx] Staged Rule Engine for native GLB kit '{robotName}' for startup sequence initialization.");
                            }
                        }
                        
                        Debug.Log($"[BaSyx] ✅ Kit {robotName} prepared via glTF (Native).");
                        StartCoroutine(SyncKitLoop(shell.id, visSubmodelId, pinSubmodelId, behSubmodelId, controlLogicSubmodelId, aidSubmodelId, robotPath, robotName, elementsJson));
                    }
                }
            }
        }
        finally
        {
            pendingRobotIds.Remove(shell.id);
            pendingRobotDynamics.Remove(shell.id);
        }
    }

    IEnumerator DownloadRecursive(string submodelId, string targetDir)
    {
        string elementsUrl = $"{aasServerUrl}/submodels/{Base64Url(submodelId)}/submodel-elements?level=deep";
        UnityWebRequest req = UnityWebRequest.Get(elementsUrl);
        yield return req.SendWebRequest();

        if (req.result != UnityWebRequest.Result.Success) yield break;

        var jsonStr = req.downloadHandler.text;
        if (string.IsNullOrEmpty(jsonStr)) yield break;
        
        var filesToDownload = new List<(string idShortPath, string value)>();
        try
        {
            var rootToken = Newtonsoft.Json.Linq.JToken.Parse(jsonStr);
            FindFilesRecursive(rootToken, "", filesToDownload);
        }
        catch (Exception e)
        {
            Debug.LogError($"[BaSyx] JSON Parse Error: {e.Message}");
            yield break;
        }

        foreach (var fileInfo in filesToDownload)
        {
            string idShortPath = fileInfo.idShortPath;
            string val = fileInfo.value;
            
            // Try to extract original filename
            string relPath = idShortPath.Split('.').Last();
            if (!string.IsNullOrEmpty(val))
            {
                string lastPart = val.Split(new[] { '/', '\\', '-' }).Last();
                if (lastPart.Contains(".")) relPath = lastPart;
            }

            string cleanPath = relPath;
            string[] prefixesToRemove = { "/aasx/robot/", "/aasx/kit/", "/aasx/", "aasx/", "robot/", "kit/" };
            foreach (var prefix in prefixesToRemove)
            {
                if (cleanPath.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                {
                    cleanPath = cleanPath.Substring(prefix.Length);
                    break;
                }
            }

            cleanPath = cleanPath.Replace("\\", "/").TrimStart('/');
            string dest = Path.Combine(targetDir, cleanPath);
            string dir = Path.GetDirectoryName(dest);
            try
            {
                if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir)) Directory.CreateDirectory(dir);
            }
            catch (Exception e)
            {
                Debug.LogError($"[BaSyx] Failed to create subdirectory {dir}: {e.Message}");
                continue;
            }

            string downloadUrl = $"{aasServerUrl}/submodels/{Base64Url(submodelId)}/submodel-elements/{idShortPath}/attachment";
            Debug.Log($"[BaSyx] 📥 Downloading: {idShortPath} -> {cleanPath}");
            
            UnityWebRequest dl = UnityWebRequest.Get(downloadUrl);
            yield return dl.SendWebRequest();

            if (dl.result == UnityWebRequest.Result.Success) {
                byte[] data = dl.downloadHandler.data;
                if (data != null && data.Length > 0) {
                    try
                    {
                        File.WriteAllBytes(dest, data);
                    }
                    catch (Exception e)
                    {
                        Debug.LogError($"[BaSyx] Failed to write downloaded file {dest}: {e.Message}");
                    }
                } else {
                    Debug.LogWarning($"[BaSyx] ⚠️ Downloaded {idShortPath} but content was empty.");
                }
            }
            else Debug.LogWarning($"[BaSyx] ⚠️ Failed to download {idShortPath}: {dl.error}");
        }
    }

    private void FindFilesRecursive(Newtonsoft.Json.Linq.JToken token, string currentPath, List<(string, string)> files)
    {
        if (token is Newtonsoft.Json.Linq.JArray arr)
        {
            foreach (var item in arr) FindFilesRecursive(item, currentPath, files);
        }
        else if (token is Newtonsoft.Json.Linq.JObject obj)
        {
            if (obj["result"] != null)
            {
                FindFilesRecursive(obj["result"], currentPath, files);
                return;
            }

            string modelType = obj["modelType"]?.ToString();
            string idShort = obj["idShort"]?.ToString();
            string newPath = string.IsNullOrEmpty(currentPath) ? idShort : $"{currentPath}.{idShort}";

            if (modelType == "File")
            {
                string value = obj["value"]?.ToString() ?? "";
                files.Add((newPath, value));
            }
            else if (modelType == "SubmodelElementCollection")
            {
                if (obj["value"] != null) FindFilesRecursive(obj["value"], newPath, files);
            }
            else if (obj["submodelElements"] != null)
            {
                FindFilesRecursive(obj["submodelElements"], newPath, files);
            }
        }
    }


    void StripPhysics(GameObject root)
    {
        var allScripts = root.GetComponentsInChildren<MonoBehaviour>(true);
        foreach (var s in allScripts)
        {
            if (s == null || s == this || s is AASRobotSync) continue;
            string typeName = s.GetType().Name;
            if (typeName.Contains("Urdf") || typeName.Contains("Articulation") || typeName.Contains("Controller")) DestroyImmediate(s);
        }
        foreach (var b in root.GetComponentsInChildren<ArticulationBody>(true)) DestroyImmediate(b);
        foreach (var rb in root.GetComponentsInChildren<Rigidbody>(true)) DestroyImmediate(rb);
        foreach (var c in root.GetComponentsInChildren<Collider>(true)) DestroyImmediate(c);
        foreach (var j in root.GetComponentsInChildren<UnityEngine.Joint>(true)) DestroyImmediate(j);
    }

    void PrepareRobot(GameObject robot, string urdfPath)
    {
        if (robot == null) return;
        StripPhysics(robot);
        Dictionary<string, MaterialData> urdfMaterials = ParseUrdfMaterials(urdfPath);

        bool isURP = UnityEngine.Rendering.GraphicsSettings.currentRenderPipeline != null;
        Shader targetShader = null;
        if (isURP)
        {
            targetShader = Shader.Find("Universal Render Pipeline/Lit");
            if (targetShader == null) targetShader = Shader.Find("Universal Render Pipeline/Simple Lit");
            if (targetShader == null) targetShader = Shader.Find("Universal Render Pipeline/Unlit");
        }
        if (targetShader == null) targetShader = Shader.Find("Standard");
        if (targetShader == null) targetShader = Shader.Find("Unlit/Texture");
        if (targetShader == null) targetShader = Shader.Find("Unlit/Color");

        foreach (var renderer in robot.GetComponentsInChildren<Renderer>())
        {
            MaterialData linkMat = null;
            Transform current = renderer.transform;
            while (current != null && current != robot.transform)
            {
                string cleanName = current.name.Replace("(Clone)", "").Trim();
                linkMat = urdfMaterials.FirstOrDefault(x => 
                    string.Equals(x.Key, cleanName, StringComparison.OrdinalIgnoreCase) || 
                    cleanName.StartsWith(x.Key, StringComparison.OrdinalIgnoreCase) ||
                    x.Key.StartsWith(cleanName, StringComparison.OrdinalIgnoreCase)).Value;
                if (linkMat != null) break;
                current = current.parent;
            }

            MeshFilter mf = renderer.GetComponent<MeshFilter>();
            bool hasVertexColors = mf != null && mf.sharedMesh != null && mf.sharedMesh.colors != null && mf.sharedMesh.colors.Length > 0;

            Material[] mats = renderer.materials;
            for (int i = 0; i < mats.Length; i++)
            {
                Material mat = mats[i];
                Texture originalTex = mat.mainTexture;
                Color originalColor = isURP ? mat.GetColor("_BaseColor") : mat.color;
                if (targetShader != null) mat.shader = targetShader;

                bool isPlaceholder = linkMat == null || !linkMat.hasColor || IsGray(linkMat.color);
                Color finalColor = originalColor;
                bool isErrorShader = (originalColor.r > 0.8f && originalColor.g < 0.2f && originalColor.b > 0.8f);

                if (originalTex != null) finalColor = Color.white;
                else if (linkMat != null && linkMat.hasColor && (!isPlaceholder || isErrorShader)) finalColor = linkMat.color;
                else if (isPlaceholder && !IsGray(originalColor) && !isErrorShader) finalColor = originalColor;
                
                if (finalColor.a < 0.01f || (isErrorShader && isPlaceholder)) finalColor = Color.gray;
                if (hasVertexColors) finalColor = Color.white;

                if (isURP)
                {
                    if (originalTex != null) mat.SetTexture("_BaseMap", originalTex);
                    mat.SetColor("_BaseColor", finalColor);
                    mat.SetFloat("_Metallic", 0.1f);
                    mat.SetFloat("_Smoothness", 0.4f);
                }
                else
                {
                    if (originalTex != null) mat.mainTexture = originalTex;
                    mat.color = finalColor;
                    mat.SetFloat("_Metallic", 0.1f);
                    mat.SetFloat("_Glossiness", 0.4f);
                }
            }
            renderer.materials = mats;
        }
    }

    IEnumerator SyncPositionLoop(string submodelId, GameObject robot)
    {
        string robotName = robot != null ? robot.name : "";
        string robotPath = Path.Combine(Application.persistentDataPath, "Robots", robotName);
        bool hasDae = false;
        if (!string.IsNullOrEmpty(robotName) && Directory.Exists(robotPath))
        {
            try {
                hasDae = Directory.GetFiles(robotPath, "*.dae", SearchOption.AllDirectories).Length > 0;
                Debug.Log($"[BaSyx] Position Sync: robot={robotName}, hasDae={hasDae}");
            } catch (Exception e) {
                Debug.LogWarning($"[BaSyx] Failed to check for DAE files: {e.Message}");
            }
        }

        while (robot != null)
        {
            string elementsUrl = $"{aasServerUrl}/submodels/{Base64Url(submodelId)}/submodel-elements";
            UnityWebRequest req = UnityWebRequest.Get(elementsUrl);
            yield return req.SendWebRequest();

            if (req.result == UnityWebRequest.Result.Success)
            {
                var elements = DeserializeList<SubmodelElement>(req.downloadHandler.text);
                Vector3 urdfPos = Vector3.zero;
                Vector3 urdfRot = Vector3.zero;
                Vector3 urdfScale = Vector3.one;
                bool xSet = false, ySet = false, zSet = false;
                bool rxSet = false, rySet = false, rzSet = false;
                bool sxSet = false, sySet = false, szSet = false;
                foreach (var el in elements)
                {
                    if (el == null || string.IsNullOrEmpty(el.idShort) || el.value == null) continue;
                    string k = el.idShort.ToUpper();
                    float val = SafeFloat(el.value);
                    if (k.StartsWith("POS_"))
                    {
                        if (k.EndsWith("X")) { urdfPos.x = val; xSet = true; }
                        else if (k.EndsWith("Y")) { urdfPos.y = val; ySet = true; }
                        else if (k.EndsWith("Z")) { urdfPos.z = val; zSet = true; }
                    }
                    else if (k.StartsWith("ROT_"))
                    {
                        if (k.EndsWith("X")) { urdfRot.x = val; rxSet = true; }
                        else if (k.EndsWith("Y")) { urdfRot.y = val; rySet = true; }
                        else if (k.EndsWith("Z")) { urdfRot.z = val; rzSet = true; }
                    }
                    else if (k.StartsWith("SCALE_"))
                    {
                        if (k.EndsWith("X")) { urdfScale.x = val; sxSet = true; }
                        else if (k.EndsWith("Y")) { urdfScale.y = val; sySet = true; }
                        else if (k.EndsWith("Z")) { urdfScale.z = val; szSet = true; }
                    }
                }
                if (xSet || ySet || zSet)
                {
                    // RH Z-up (AAS) to LH Y-up (Unity)
                    // Unity X = -URDF Y
                    // Unity Y =  URDF Z
                    // Unity Z =  URDF X
                    robot.transform.position = new Vector3(-urdfPos.y, urdfPos.z, urdfPos.x);
                }
                if (rxSet || rySet || rzSet)
                {
                    // Direct coordinate mapping to align exactly with Python.
                    // We apply a base 90-degree offset to align the robot's forward direction with the layout.
                    // If the robot contains DAE meshes, we apply an additional 180-degree rotation (total 270)
                    // at the root level to compensate for the Collada coordinate conversion.
                    float yaw = 90f - urdfRot.z;
                    if (hasDae) yaw += 180f;
                    robot.transform.eulerAngles = new Vector3(-urdfRot.y, yaw, urdfRot.x);
                }
                if (sxSet || sySet || szSet)
                {
                    robot.transform.localScale = new Vector3(urdfScale.y, urdfScale.z, urdfScale.x) * globalScale;
                }
            }
            yield return new WaitForSeconds(positionSyncInterval);
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

    bool IsGray(Color c)
    {
        float avg = (c.r + c.g + c.b) / 3f;
        return Mathf.Abs(c.r - avg) < 0.02f && Mathf.Abs(c.g - avg) < 0.02f && Mathf.Abs(c.b - avg) < 0.02f;
    }

    Color ParseRGBA(string rgba)
    {
        if (string.IsNullOrEmpty(rgba)) return Color.gray;
        try
        {
            var p = rgba.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries)
                        .Select(s => float.Parse(s, System.Globalization.CultureInfo.InvariantCulture))
                        .ToArray();
            return p.Length >= 3 ? new Color(p[0], p[1], p[2], p.Length > 3 ? p[3] : 1f) : Color.gray;
        }
        catch { return Color.gray; }
    }

    Dictionary<string, MaterialData> ParseUrdfMaterials(string path)
    {
        var materials = new Dictionary<string, MaterialData>();
        try
        {
            XDocument doc = XDocument.Load(path);
            var globals = new Dictionary<string, MaterialData>();
            foreach (var m in doc.Descendants("material"))
            {
                string n = m.Attribute("name")?.Value;
                if (string.IsNullOrEmpty(n) || globals.ContainsKey(n)) continue;
                var data = new MaterialData();
                var c = m.Element("color");
                if (c != null) { data.color = ParseRGBA(c.Attribute("rgba")?.Value); data.hasColor = true; }
                if (data.hasColor) globals[n] = data;
            }
            foreach (var l in doc.Descendants("link"))
            {
                string ln = l.Attribute("name")?.Value;
                if (string.IsNullOrEmpty(ln)) continue;
                foreach (var v in l.Elements("visual"))
                {
                    var m = v.Element("material");
                    if (m == null) continue;
                    string mn = m.Attribute("name")?.Value;
                    var data = new MaterialData();
                    var c = m.Element("color");
                    if (c != null) { data.color = ParseRGBA(c.Attribute("rgba")?.Value); data.hasColor = true; }
                    if (!data.hasColor && !string.IsNullOrEmpty(mn) && globals.ContainsKey(mn)) data = globals[mn];
                    if (data.hasColor) materials[ln] = data;
                }
            }
        }
        catch { }
        return materials;
    }

    [Serializable] public class AasDescriptor { public string idShort; public string id; public List<SubmodelDescriptor> submodelDescriptors; }
    [Serializable] public class SubmodelDescriptor { public string idShort; public string id; }
    [Serializable] public class SubmodelElement { public string idShort; public string modelType; public object value; }
    [Serializable] public class AasRepositoryModel { public List<SubmodelReference> submodels; }
    [Serializable] public class SubmodelReference { public List<SubmodelKey> keys; }
    [Serializable] public class SubmodelKey { public string value; }
    [Serializable] public class PagedResponse<T> { public List<T> result; }

    private List<T> DeserializeList<T>(string json)
    {
        if (string.IsNullOrEmpty(json)) return new List<T>();
        if (json.Trim().StartsWith("{")) return JsonConvert.DeserializeObject<PagedResponse<T>>(json)?.result ?? new List<T>();
        return JsonConvert.DeserializeObject<List<T>>(json) ?? new List<T>();
    }

    string Base64Url(string i) => Convert.ToBase64String(Encoding.UTF8.GetBytes(i)).Replace("+", "-").Replace("/", "_").TrimEnd('=');
    string FindUrdfFile(string dir) 
    { 
        var fs = Directory.GetFiles(dir, "*.*", SearchOption.AllDirectories)
                .Where(f => f.EndsWith(".urdf", StringComparison.OrdinalIgnoreCase) || f.EndsWith(".urdf.xacro", StringComparison.OrdinalIgnoreCase))
                .ToArray();
        if (fs.Length == 0) return null;
        return fs.OrderByDescending(f => f.Contains("robot")).ThenByDescending(f => f.EndsWith(".urdf", StringComparison.OrdinalIgnoreCase)).First();
    }

    IEnumerator SyncKitLoop(string shellId, string visSubmodelId, string pinSubmodelId, string behSubmodelId, string controlLogicSubmodelId, string aidSubmodelId, string robotPath, string robotName, string initialJson)
    {
        string lastJson = initialJson;
        string elementsUrl = $"{aasServerUrl}/submodels/{Base64Url(visSubmodelId)}/submodel-elements?level=deep";
        
        while (instantiatedRobots.ContainsKey(shellId))
        {
            yield return new WaitForSeconds(discoveryInterval);
            
            if (!instantiatedRobots.ContainsKey(shellId)) yield break;
            
            UnityWebRequest req = UnityWebRequest.Get(elementsUrl);
            yield return req.SendWebRequest();
            
            if (req.result == UnityWebRequest.Result.Success)
            {
                string currentJson = req.downloadHandler.text;
                if (currentJson != lastJson)
                {
                    Debug.Log($"[BaSyx] 🔄 Layout change detected for Kit '{robotName}'. Hot-reloading...");
                    lastJson = currentJson;
                    
                    // 1. Destroy old instance
                    GameObject oldKit = instantiatedRobots[shellId];
                    if (oldKit != null)
                    {
                        if (RobotCameraManager.Instance != null) RobotCameraManager.Instance.RemoveTarget(oldKit.transform);
                        Destroy(oldKit);
                    }
                    
                    // 2. Download any new assets
                    yield return StartCoroutine(DownloadRecursive(visSubmodelId, robotPath));
                    
                    // 3. Re-import
                    var task = AASKitImporter.ImportWithMetadataAsync(currentJson, robotPath, robotName);
                    yield return new WaitUntil(() => task.IsCompleted);
                    var importData = task.Result;
                    
                    GameObject newKit = importData?.root;
                    if (newKit != null)
                    {
                        GameObject container = GameObject.Find("AAS_Kits_Container");
                        if (container == null) container = new GameObject("AAS_Kits_Container");
                        newKit.transform.SetParent(container.transform, false);
                        newKit.transform.localScale = Vector3.one * globalScale;
                        
                        instantiatedRobots[shellId] = newKit;
                        if (RobotCameraManager.Instance != null) RobotCameraManager.Instance.AddTarget(newKit.transform);
                        
                        // Parse AID during hot-reload
                        string kitProtocol = "tcp";
                        string kitEndpointIP = "";
                        int kitEndpointPort = 8888;
                        string skillServerURL = "";

                        if (aidSubmodelId != null)
                        {
                            string aidUrl = $"{aasServerUrl}/submodels/{Base64Url(aidSubmodelId)}/submodel-elements?level=deep";
                            UnityWebRequest aidReq = UnityWebRequest.Get(aidUrl);
                            yield return aidReq.SendWebRequest();
                            if (aidReq.result == UnityWebRequest.Result.Success)
                            {
                                try
                                {
                                    var elements = DeserializeList<SubmodelElement>(aidReq.downloadHandler.text);
                                    foreach (var el in elements)
                                    {
                                        if (el.idShort == "Protocol" && el.value != null) kitProtocol = el.value.ToString();
                                        if (el.idShort == "EndpointIP" && el.value != null) kitEndpointIP = el.value.ToString();
                                        if (el.idShort == "EndpointPort" && el.value != null) int.TryParse(el.value.ToString(), out kitEndpointPort);
                                    }
                                    if (!string.IsNullOrEmpty(kitEndpointIP)) skillServerURL = $"http://{kitEndpointIP}:80";
                                }
                                catch (Exception ex)
                                {
                                    Debug.LogError($"[BaSyx] Hot-reload AID parsing error: {ex.Message}");
                                }
                            }
                        }

                        if (pinSubmodelId != null && behSubmodelId != null)
                        {
                            var actuatorCtrl = newKit.AddComponent<AASActuatorController>();
                            actuatorCtrl.Init(aasServerUrl, pinSubmodelId, behSubmodelId, importData.components, kitProtocol, kitEndpointIP, kitEndpointPort);
                            
                            var sensorCtrl = newKit.AddComponent<AASSensorController>();
                            sensorCtrl.Init(aasServerUrl, pinSubmodelId, behSubmodelId, importData.components, kitProtocol, kitEndpointIP, kitEndpointPort);

                            if (controlLogicSubmodelId != null)
                            {
                                var engine = newKit.AddComponent<AASRuleEngine>();
                                engine.warningPanel = warningPanel;
                                engine.warningTextTMP = warningTextTMP;
                                var driver = new AASHttpDriver(sensorCtrl, actuatorCtrl, aasServerUrl, pinSubmodelId);
                                engine.Init(aasServerUrl, controlLogicSubmodelId, driver, skillServerURL);
                                Debug.Log($"[BaSyx] Rule engine re-attached to hot-reloaded kit '{robotName}' with DT skill URL: {skillServerURL}");
                            }
                        }
                        
                        Debug.Log($"[BaSyx] 🔄 Hot-reload complete for Kit '{robotName}'.");
                    }
                }
            }
        }
    }

    private bool CheckIsDynamicFromJson(string jsonElements)
    {
        try
        {
            JToken rootToken = JToken.Parse(jsonElements);
            JArray elements = null;
            
            if (rootToken is JObject obj && obj["result"] != null) 
                elements = obj["result"] as JArray;
            else if (rootToken is JArray arr) 
                elements = arr;

            if (elements == null) return false;

            foreach (var token in elements)
            {
                var valueNode = token["value"];
                if (valueNode != null && valueNode.Type == JTokenType.Array)
                {
                    foreach (var prop in valueNode)
                    {
                        string idShort = prop["idShort"]?.ToString();
                        if (idShort != null && idShort.Replace(" ", "").ToLower() == "dynamicobject")
                        {
                            if (prop["value"]?.ToString().ToLower() == "true")
                            {
                                return true;
                            }
                        }
                    }
                }
            }
        }
        catch (Exception e)
        {
            Debug.LogError($"[BaSyx] Error parsing DynamicObject: {e.Message}");
        }
        return false;
    }

    private bool CheckCanHaveMultipleFromJson(string jsonElements)
    {
        try
        {
            JToken rootToken = JToken.Parse(jsonElements);
            JArray elements = null;
            if (rootToken is JObject obj && obj["result"] != null) 
                elements = obj["result"] as JArray;
            else if (rootToken is JArray arr) 
                elements = arr;

            if (elements == null) return false;

            foreach (var token in elements)
            {
                var valueNode = token["value"];
                if (valueNode != null && valueNode.Type == JTokenType.Array)
                {
                    foreach (var prop in valueNode)
                    {
                        string idShort = prop["idShort"]?.ToString();
                        if (idShort != null && idShort.Replace(" ", "").ToLower() == "canhavemultiple")
                        {
                            if (prop["value"]?.ToString().ToLower() == "true")
                            {
                                return true;
                            }
                        }
                    }
                }
            }
        }
        catch {}
        return false;
    }


    IEnumerator ResetVirtualPinsRoutine(string pinSubmodelId)
    {
        string url = $"{aasServerUrl}/submodels/{Base64Url(pinSubmodelId)}/submodel-elements?level=deep";
        UnityWebRequest req = UnityWebRequest.Get(url);
        yield return req.SendWebRequest();

        if (req.result != UnityWebRequest.Result.Success)
        {
            Debug.LogWarning($"[BaSyx] Failed to fetch VirtualPins for reset: {req.error}");
            yield break;
        }

        string json = req.downloadHandler.text;
        JArray elements = null;
        try
        {
            if (json.Trim().StartsWith("{"))
            {
                var paged = JObject.Parse(json);
                elements = paged["result"] as JArray;
            }
            else
            {
                elements = JArray.Parse(json);
            }
        }
        catch (Exception e)
        {
            Debug.LogError($"[BaSyx] JSON Parse error for VirtualPins reset: {e.Message}");
        }

        if (elements == null) yield break;

        foreach (var element in elements)
        {
            string idShort = element["idShort"]?.ToString();
            if (idShort == "Inputs" || idShort == "Outputs")
            {
                var values = element["value"] as JArray;
                if (values != null)
                {
                    foreach (var val in values)
                    {
                        string pinId = val["idShort"]?.ToString();
                        string modelType = val["modelType"]?.ToString();
                        if (modelType == "Property" && !string.IsNullOrEmpty(pinId))
                        {
                            string putUrl = $"{aasServerUrl}/submodels/{Base64Url(pinSubmodelId)}/submodel-elements/{idShort}.{pinId}";
                            string putJson = $"{{\"idShort\":\"{pinId}\",\"modelType\":\"Property\",\"valueType\":\"xs:boolean\",\"value\":\"false\"}}";
                            
                            UnityWebRequest putReq = new UnityWebRequest(putUrl, "PUT");
                            byte[] bodyRaw = Encoding.UTF8.GetBytes(putJson);
                            putReq.uploadHandler = new UploadHandlerRaw(bodyRaw);
                            putReq.downloadHandler = new DownloadHandlerBuffer();
                            putReq.SetRequestHeader("Content-Type", "application/json");
                            putReq.timeout = 2;
                            
                            yield return putReq.SendWebRequest();
                        }
                    }
                }
            }
        }
        Debug.Log($"[BaSyx] Reset all virtual pins to false for submodel: {pinSubmodelId}");
    }
}

public class AASProductConfig : UnityEngine.MonoBehaviour
{
    public bool canHaveMultiple;
}