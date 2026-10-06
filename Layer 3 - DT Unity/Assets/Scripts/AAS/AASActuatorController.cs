using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.Networking;
using Newtonsoft.Json.Linq;

public class AASActuatorController : MonoBehaviour
{
    private string pinsUrl;
    private string behaviorUrl;
    private float pollInterval = 0.05f; // 20Hz

    [Serializable]
    public class ActuatorBinding
    {
        public string pinId;
        public GameObject target;
        public string type;
        public Vector3 axis;
        public float speed;
        public float limitMin;
        public float limitMax;
        public bool currentState;
        public bool realState; // Real-world state from Arduino TCP Sockets
        
        public Vector3 initialPosition;
        public Quaternion initialRotation;
        public Vector2 currentOffset;
        
        public Vector3 lastPosition;
        public Quaternion lastRotation;
        
        public Vector3 deltaPosition;
        public Quaternion deltaRotation = Quaternion.identity;
        
        public string stopSensorComponent;
        public AASSensorController.SensorPhysicsProxy linkedSensor;

        // Real-world socket connection link (Digital Twin)
        public bool realActuatorEnabled;
        public string realPortId; // e.g. "R0_0"
    }

    public List<ActuatorBinding> bindings = new List<ActuatorBinding>();
    private bool isInitialized = false;
    private bool isStabilized = false;

    // --- Digital Twin Actuator TCP connection ---
    private string _kitProtocol = "tcp";
    private string _kitEndpointIP = "";
    private int _kitEndpointPort = 8888;
    private bool _dtActive = false;
    private Dictionary<string, Action<bool>> _portCallbacks = new Dictionary<string, Action<bool>>();

    private static PhysicsMaterial _highFrictionMaterial;
    private static PhysicsMaterial HighFrictionMaterial {
        get {
            if (_highFrictionMaterial == null) {
                _highFrictionMaterial = new PhysicsMaterial("HighFriction") {
                    dynamicFriction = 1.0f,
                    staticFriction = 1.0f,
                    frictionCombine = PhysicsMaterialCombine.Maximum,
                    bounceCombine = PhysicsMaterialCombine.Minimum
                };
            }
            return _highFrictionMaterial;
        }
    }

    // --- Product-Centric Movement ---
    public class ConveyorContactTracker : MonoBehaviour {
        private Rigidbody rb;
        public AASActuatorController actuatorCtrl;
        private class ContactInfo {
            public float lastTime;
            public List<ActuatorBinding> bindings;
        }
        private Dictionary<GameObject, ContactInfo> activeContacts = new Dictionary<GameObject, ContactInfo>();

        void Awake() { rb = GetComponent<Rigidbody>(); }

        public void NotifyContact(GameObject root, List<ActuatorBinding> kitBindings) {
            if (!activeContacts.ContainsKey(root)) {
                activeContacts[root] = new ContactInfo { bindings = kitBindings };
            }
            activeContacts[root].lastTime = Time.fixedTime;
        }

        void FixedUpdate() {
            var expired = activeContacts.Where(kvp => Time.fixedTime - kvp.Value.lastTime > 0.2f).Select(kvp => kvp.Key).ToList();
            foreach (var key in expired) activeContacts.Remove(key);

            if (activeContacts.Count == 0) return;

            Vector3 deltaTranslation = Vector3.zero;
            Quaternion deltaRotation = Quaternion.identity;
            List<Vector3> candidateVelocities = new List<Vector3>();

            foreach (var kvp in activeContacts) {
                GameObject root = kvp.Key;
                ContactInfo contact = kvp.Value;
                
                Vector3 combinedDeltaPos = Vector3.zero;
                Quaternion combinedDeltaRot = Quaternion.identity;
                Vector3 conveyorAxis = Vector3.forward;
                float conveyorSpeed = 0f;
                bool conveyorState = false;
                AASSensorController.SensorPhysicsProxy conveyorSensor = null;

                Transform curr = root.transform;
                bool foundBinding = false;
                while (curr != null) {
                    var matches = contact.bindings.Where(b => b.target == curr.gameObject).ToList();
                    if (matches.Count > 0) {
                        foreach (var b in matches) {
                            combinedDeltaPos += b.deltaPosition;
                            combinedDeltaRot = b.deltaRotation * combinedDeltaRot;
                            
                            if (b.type == "Conveyor") {
                                if (!actuatorCtrl.isStabilized) {
                                    conveyorState = false;
                                    conveyorSpeed = 0f;
                                } else {
                                    if (b.currentState || conveyorSpeed == 0f) {
                                        conveyorAxis = b.axis;
                                        conveyorSpeed = b.speed;
                                        conveyorState = b.currentState;
                                        conveyorSensor = b.linkedSensor;
                                        
                                        // Calculate the correct world direction using the conveyor sub-component's transform
                                        Vector3 worldDir = b.target.transform.TransformDirection(b.axis);
                                        bool isBlocked = conveyorSensor != null && (conveyorSensor.binding.activeLow ? !conveyorSensor.binding.lastState : conveyorSensor.binding.lastState);
                                        Vector3 vel = (conveyorState && !isBlocked) ? (worldDir * conveyorSpeed) : Vector3.zero;
                                        candidateVelocities.Add(vel);
                                    }
                                }
                            }
                        }
                        foundBinding = true;
                        break; 
                    }
                    curr = curr.parent;
                }
                if (foundBinding) {
                    deltaTranslation += combinedDeltaPos;
                    deltaRotation = combinedDeltaRot * deltaRotation;
                }
            }

            Vector3 targetPosition = rb.position;
            Quaternion targetRotation = rb.rotation;
            bool hasMovement = false;

            // Apply platform translation & rotation delta
            if (deltaTranslation.sqrMagnitude > 0.0001f || Quaternion.Angle(deltaRotation, Quaternion.identity) > 0.01f) {
                Vector3 pivot = rb.position;
                var firstContact = activeContacts.Keys.FirstOrDefault();
                if (firstContact != null) {
                    pivot = firstContact.transform.position;
                }
                
                Vector3 relativeToPivot = rb.position - pivot;
                Vector3 rotatedPos = deltaRotation * relativeToPivot;
                Vector3 rotDisplacement = rotatedPos - relativeToPivot;

                targetPosition += deltaTranslation + rotDisplacement;
                targetRotation = deltaRotation * targetRotation;
                hasMovement = true;
            }

            // Apply conveyor surface belt speed
            Vector3 targetVel = Vector3.zero;
            var activeVels = candidateVelocities.Where(v => v.sqrMagnitude > 0.001f).ToList();

            if (activeVels.Count > 0) {
                bool opposing = false;
                for (int i = 0; i < activeVels.Count; i++) {
                    for (int j = i + 1; j < activeVels.Count; j++) {
                        if (Vector3.Dot(activeVels[i].normalized, activeVels[j].normalized) < -0.1f) {
                            opposing = true; break;
                        }
                    }
                    if (opposing) break;
                }
                if (opposing) {
                    targetVel = Vector3.zero;
                } else {
                    targetVel = activeVels[0];
                    foreach (var v in activeVels) {
                        if (v.magnitude > targetVel.magnitude) targetVel = v;
                    }
                }
            }

            if (targetVel.sqrMagnitude > 0.001f) {
                float velocityY = rb.isKinematic ? 0f : rb.linearVelocity.y;
                Vector3 finalVel = new Vector3(targetVel.x, velocityY, targetVel.z);
                
                if (!rb.isKinematic) {
                    if (rb.IsSleeping()) rb.WakeUp();
                    rb.linearDamping = 0;
                    rb.linearVelocity = finalVel;
                }
                
                targetPosition += finalVel * Time.fixedDeltaTime;
                hasMovement = true;
            } else {
                if (!rb.isKinematic) {
                    rb.linearVelocity = new Vector3(0, rb.linearVelocity.y, 0);
                }
            }

            if (hasMovement) {
                rb.MovePosition(targetPosition);
                rb.MoveRotation(targetRotation);
                
                // Clear physics angular velocity during kinematic sweep to prevent force buildup
                if (!rb.isKinematic) {
                    rb.angularVelocity = Vector3.zero;
                }
            } else {
                // Stopped: zero out angular velocity to completely eliminate rotation drift/creep from collisions
                if (!rb.isKinematic) {
                    rb.angularVelocity = Vector3.zero;
                }
            }

            foreach (var col in GetComponentsInChildren<Collider>()) col.contactOffset = 0.001f;
        }
    }

    public class ConveyorSurface : MonoBehaviour {
        public GameObject rootConveyor;
        private AASActuatorController controller;
        public void Setup(GameObject root, AASActuatorController ctrl) {
            rootConveyor = root;
            controller = ctrl;
        }
        void Start() {
            var col = GetComponent<Collider>();
            if (col != null) col.material = HighFrictionMaterial;
        }
        private void Register(GameObject other) {
            var rb = other.GetComponentInParent<Rigidbody>();
            if (rb != null) {
                // Only move actual dynamic products (e.g. materials/parts), never sensors or static parts of other kits
                if (rb.GetComponent<AAS.DynamicProduct>() == null && 
                    rb.GetComponentInChildren<AAS.DynamicProduct>() == null) return;

                // If the Rigidbody belongs to the conveyor kit itself, ignore it to prevent moving sensors/rollers.
                if (rb.transform.IsChildOf(controller.transform)) return;

                var tracker = rb.GetComponent<ConveyorContactTracker>() ?? rb.gameObject.AddComponent<ConveyorContactTracker>();
                tracker.actuatorCtrl = controller;
                tracker.NotifyContact(rootConveyor, controller.bindings);
            }
        }
        void OnCollisionStay(Collision c) { Register(c.gameObject); }
        void OnTriggerStay(Collider other) { Register(other.gameObject); }
    }

    private void Start()
    {
        OperationModeManager.OnModeChanged += OnOperationModeChanged;
    }

    private void OnDestroy()
    {
        OperationModeManager.OnModeChanged -= OnOperationModeChanged;
        StopDTConnection();
    }

    private void OnOperationModeChanged(OperationMode mode) => ApplyOperationMode(mode);

    private void ApplyOperationMode(OperationMode mode)
    {
        if (!isInitialized) return;
        bool isVC = mode == OperationMode.VirtualCommissioning;

        StopDTConnection();
        if (!isVC)
        {
            if (!string.IsNullOrEmpty(_kitEndpointIP))
            {
                StartDTConnection();
            }
        }
        Debug.Log($"[Actuator] 🔄 Mode → {mode} on '{gameObject.name}'");
    }

    private void StartDTConnection()
    {
        if (OperationModeManager.Current == OperationMode.DigitalTwin && !BaSyxManager.IsStartupComplete)
        {
            Debug.Log($"[Actuator] ⏳ DT Connection for '{gameObject.name}' delayed until BaSyxManager startup is complete.");
            StartCoroutine(WaitAndStartDTConnection());
            return;
        }

        string proto = (_kitProtocol ?? "tcp").ToLower().Trim();
        if (proto != "tcp" && proto != "socket" && proto != "sockets" && proto != "none") return;

        _dtActive = true;

        if (AASArduinoLink.Instance == null)
        {
            var go = new GameObject("AASArduinoLink");
            go.AddComponent<AASArduinoLink>();
        }

        AASArduinoLink.Instance.StartConnection(_kitEndpointIP, _kitEndpointPort);

        foreach (var binding in bindings)
        {
            if (!binding.realActuatorEnabled || string.IsNullOrEmpty(binding.realPortId)) continue;

            var b = binding;
            Action<bool> cb = (value) => OnArduinoPortChanged(b, value);
            _portCallbacks[binding.realPortId] = cb;
            AASArduinoLink.Instance.Subscribe(binding.realPortId, cb);
        }

        Debug.Log($"[Actuator] 🔗 DT subscribed to ArduinoLink ({_kitEndpointIP}:{_kitEndpointPort}) for {_portCallbacks.Count} actuator port(s) on '{gameObject.name}'");
    }

    private IEnumerator WaitAndStartDTConnection()
    {
        while (!BaSyxManager.IsStartupComplete)
        {
            yield return new WaitForSeconds(0.5f);
        }

        if (OperationModeManager.Current == OperationMode.DigitalTwin && _dtActive == false)
        {
            Debug.Log($"[Actuator] 🚀 BaSyxManager startup complete. Activating DT Connection for '{gameObject.name}'.");
            StartDTConnection();
        }
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
    }

    private void OnArduinoPortChanged(ActuatorBinding binding, bool value)
    {
        if (!_dtActive) return;
        binding.realState = value;
        binding.currentState = value; // Overwrite simulator local state to force matching visual movement
        Debug.Log($"[Actuator] 📡 DT Actuator state received: '{binding.pinId}' ({binding.realPortId}) = {value}. Overwrote simulator state!");
        
        // Push state change to AAS so that it remains in sync with the hardware socket
        StartCoroutine(UpdateActuatorAASAsync(binding.pinId, value));
    }

    private IEnumerator UpdateActuatorAASAsync(string pinId, bool value)
    {
        string url = $"{pinsUrl}.{pinId}";
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
                Debug.LogWarning($"[Actuator] ❌ Failed to sync socket value for {pinId} to AAS: {req.error} (URL: {url})");
            }
        }
    }

    public void Init(string serverUrl, string pinsSubmodelId, string behaviorSubmodelId, Dictionary<string, GameObject> componentMap, 
                    string protocol = "tcp", string ip = "", int port = 8888, float delay = 10.0f)
    {
        this.pinsUrl = $"{serverUrl}/submodels/{Base64Url(pinsSubmodelId)}/submodel-elements/Outputs";
        this.behaviorUrl = $"{serverUrl}/submodels/{Base64Url(behaviorSubmodelId)}/submodel-elements";
        this._kitProtocol = protocol;
        this._kitEndpointIP = ip;
        this._kitEndpointPort = port;
        isStabilized = true;
        StartCoroutine(LoadBindingsRoutine(componentMap));
    }

    IEnumerator LoadBindingsRoutine(Dictionary<string, GameObject> componentMap)
    {
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
                    Vector3 axis = Vector3.forward;
                    float speed = 1f;
                    float min = 0f, max = 0f;
                    string stopSensorComponent = null;
                    bool realActuatorEnabled = false;
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
                                float fv = 0f;
                                if (p["value"] != null) float.TryParse(p["value"].ToString().Replace(',', '.'), System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out fv);
                                if (pid == "AxisX") axis.x = fv;
                                if (pid == "AxisY") axis.y = fv;
                                if (pid == "AxisZ") axis.z = fv;
                                if (pid == "Speed") speed = fv;
                                if (pid == "LimitMin") min = fv;
                                if (pid == "LimitMax") max = fv;
                                if (pid == "StopSensorComponent") stopSensorComponent = p["value"]?.ToString();
                                if (pid == "RealActuatorEnabled") realActuatorEnabled = p["value"]?.ToString().ToLower() == "true";
                                if (pid == "RealPortID") realPortId = p["value"]?.ToString();
                            }
                        }
                    }
                    if (type == "TriggerZone" || type == "ContactSensor") continue;
                    
                    GameObject target = FindTargetComponent(componentMap, compName);

                    if (target != null && !string.IsNullOrEmpty(type))
                    {
                        // Swap Y and Z to match existing AAS configuration files
                        Vector3 unityAxis = new Vector3(axis.x, axis.z, axis.y).normalized;
                        var binding = new ActuatorBinding {
                            pinId = pinId, target = target, type = type, axis = unityAxis,
                            speed = speed, limitMin = min, limitMax = max,
                            initialPosition = target.transform.localPosition,
                            initialRotation = target.transform.localRotation,
                            lastPosition = target.transform.position,
                            lastRotation = target.transform.rotation,
                            stopSensorComponent = stopSensorComponent,
                            realActuatorEnabled = realActuatorEnabled,
                            realPortId = realPortId
                        };
                        bindings.Add(binding);
                        if (type == "Conveyor" || type == "RotateContinuous" || type == "TranslateContinuous") {
                            foreach (var col in target.GetComponentsInChildren<Collider>()) {
                                if (col.gameObject.GetComponent<Rigidbody>() == null) {
                                    var rb = col.gameObject.AddComponent<Rigidbody>();
                                    rb.isKinematic = true;
                                }
                                var surf = col.gameObject.GetComponent<ConveyorSurface>() ?? col.gameObject.AddComponent<ConveyorSurface>();
                                surf.Setup(target, this);
                            }
                        }
                    }
                }
            }
            isInitialized = true;
            
            var sensorProxies = FindObjectsByType<AASSensorController.SensorPhysicsProxy>(FindObjectsSortMode.None);
            foreach (var b in bindings) {
                if (!string.IsNullOrEmpty(b.stopSensorComponent)) {
                    b.linkedSensor = sensorProxies.FirstOrDefault(s => 
                        s.binding.componentName == b.stopSensorComponent || 
                        s.binding.pinId == b.stopSensorComponent);
                }
            }

            StartCoroutine(PollingLoop());
            
            // Connect to real hardware actuators if in DT mode
            ApplyOperationMode(OperationModeManager.Current);
        }
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


    IEnumerator PollingLoop()
    {
        while (true)
        {
            if (OperationModeManager.Current == OperationMode.DigitalTwin)
            {
                yield return new WaitForSeconds(1.0f);
                continue;
            }

            UnityWebRequest req = UnityWebRequest.Get(pinsUrl + "?level=deep");
            yield return req.SendWebRequest();
            if (req.result == UnityWebRequest.Result.Success)
            {
                JToken root = JToken.Parse(req.downloadHandler.text);
                JArray elements = null;
                if (root is JObject obj) elements = (obj["result"] ?? obj["value"]) as JArray;
                else if (root is JArray arr) elements = arr;
                if (elements != null)
                {
                    foreach (var el in elements)
                    {
                        string pid = el["idShort"]?.ToString();
                        bool val = false;
                        if (el["value"] != null) bool.TryParse(el["value"].ToString(), out val);
                        foreach (var b in bindings.Where(x => x.pinId == pid)) {
                            b.currentState = val;
                        }
                    }
                }
            }
            yield return new WaitForSeconds(pollInterval);
        }
    }

    private bool IsBlockedBySensor(ActuatorBinding b)
    {
        if (b.linkedSensor == null && !string.IsNullOrEmpty(b.stopSensorComponent))
        {
            var sensorProxies = FindObjectsByType<AASSensorController.SensorPhysicsProxy>(FindObjectsSortMode.None);
            b.linkedSensor = sensorProxies.FirstOrDefault(s => 
                s.binding.componentName == b.stopSensorComponent || 
                s.binding.pinId == b.stopSensorComponent);
            
            if (b.linkedSensor != null) {
                Debug.Log($"[Actuator] 🔗 Dynamically linked {b.pinId} to sensor {b.stopSensorComponent}");
            }
        }

        if (b.linkedSensor != null)
        {
            return b.linkedSensor.binding.lastState;
        }
        return false;
    }

    void FixedUpdate()
    {
        if (!isInitialized || !isStabilized) return;
        if (OperationModeManager.Current == OperationMode.DigitalTwin && !BaSyxManager.IsStartupComplete) return;

        foreach (var b in bindings) {
            if (b.target == null) continue;
            
            Vector3 posBefore = b.target.transform.position;
            Quaternion rotBefore = b.target.transform.rotation;
            
            bool isBlocked = IsBlockedBySensor(b);
            if (!(isBlocked && b.currentState)) {
                switch (b.type) {
                    case "TranslateContinuous": 
                        if (b.currentState) b.target.transform.Translate(b.axis * b.speed * Time.fixedDeltaTime, Space.Self); 
                        break;
                    case "RotateContinuous": 
                        if (b.currentState) {
                            b.target.transform.Rotate(b.axis, b.speed * Time.fixedDeltaTime * 200f, Space.Self); 
                        }
                        break;
                    case "TranslateFixed":
                        Vector3 targetPos = b.initialPosition + (b.axis * (b.currentState ? b.limitMax : b.limitMin));
                        b.target.transform.localPosition = Vector3.Lerp(b.target.transform.localPosition, targetPos, Time.fixedDeltaTime * b.speed * 5f);
                        break;
                    case "RotateHinge":
                        float targetAngle = b.currentState ? b.limitMax : b.limitMin;
                        Quaternion targetRot = b.initialRotation * Quaternion.AngleAxis(targetAngle, b.axis);
                        b.target.transform.localRotation = Quaternion.Slerp(b.target.transform.localRotation, targetRot, Time.fixedDeltaTime * b.speed * 5f);
                        break;
                }
            }
            
            b.deltaPosition = b.target.transform.position - posBefore;
            b.deltaRotation = b.target.transform.rotation * Quaternion.Inverse(rotBefore);
            
            if (b.deltaPosition.sqrMagnitude < 1e-8f) {
                b.deltaPosition = Vector3.zero;
            }
            if (Quaternion.Angle(b.deltaRotation, Quaternion.identity) < 0.005f) {
                b.deltaRotation = Quaternion.identity;
            }
        }

        // Keep last positions updated for product physics delta tracking
        foreach (var b in bindings) {
            if (b.target == null) continue;
            b.lastPosition = b.target.transform.position;
            b.lastRotation = b.target.transform.rotation;
        }
    }

    string Base64Url(string i) => Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(i)).Replace("+", "-").Replace("/", "_").TrimEnd('=');
}