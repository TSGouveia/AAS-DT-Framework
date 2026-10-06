using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using UnityEngine;
using Newtonsoft.Json.Linq;
using GLTFast;

public class AASKitImporter : MonoBehaviour
{
    public class KitImportData
    {
        public GameObject root;
        public Dictionary<string, GameObject> components;
    }

    public static async Task<KitImportData> ImportWithMetadataAsync(string jsonElements, string kitPath, string kitName, Transform parent = null)
    {
        try {
            JToken rootToken = JToken.Parse(jsonElements);
            JArray elements = null;
            
            if (rootToken is JObject obj && obj["result"] != null) elements = obj["result"] as JArray;
            else if (rootToken is JArray arr) elements = arr;

            if (elements == null) return null;

            GameObject kitRoot = new GameObject(kitName);
            if (parent != null) kitRoot.transform.SetParent(parent, false);

            Dictionary<string, GameObject> components = new Dictionary<string, GameObject>();
            Dictionary<string, string> parentMap = new Dictionary<string, string>();

            HashSet<string> dynamicComponents = new HashSet<string>();

            // 1. First Pass: Create/Find GameObjects and Load GLBs
            foreach (JToken token in elements)
            {
                string compName = token["idShort"]?.ToString();
                if (string.IsNullOrEmpty(compName)) continue;

                string meshFile = "";
                string parentName = "";
                Vector3 pos = Vector3.zero;
                Vector3 rot = Vector3.zero;
                Vector3 scale = Vector3.one;

                JToken valueNode = token["value"];
                if (valueNode != null && valueNode.Type == JTokenType.Array)
                {
                    foreach (JToken prop in valueNode)
                    {
                        string idShort = prop["idShort"]?.ToString();
                        if (idShort == "Mesh") meshFile = prop["value"]?.ToString();
                        if (idShort == "Parent") parentName = prop["value"]?.ToString();
                        if (idShort != null && idShort.Replace(" ", "").ToLower() == "dynamicobject")
                        {
                            if (prop["value"]?.ToString().ToLower() == "true")
                            {
                                dynamicComponents.Add(compName);
                            }
                        }
                        if (idShort == "Transform")
                        {
                            foreach (JToken tp in prop["value"])
                            {
                                string tId = tp["idShort"]?.ToString();
                                float v = SafeFloat(tp["value"]);
                                if (tId == "PosX") pos.x = v; if (tId == "PosY") pos.y = v; if (tId == "PosZ") pos.z = v;
                                if (tId == "RotX") rot.x = v; if (tId == "RotY") rot.y = v; if (tId == "RotZ") rot.z = v;
                                if (tId == "ScaleX") scale.x = v; if (tId == "ScaleY") scale.y = v; if (tId == "ScaleZ") scale.z = v;
                            }
                        }
                    }
                }

                GameObject compObj = null;
                bool wasPreExisting = false;
                
                // CRITICAL FIX: Search for this component name across ALL already loaded 
                // mesh hierarchies in this kit BEFORE creating a new one.
                foreach (var existing in components.Values) {
                    if (existing == null) continue;
                    // Check if the 'existing' object itself is the target, or one of its children
                    if (existing.name == compName) {
                        compObj = existing;
                        wasPreExisting = true;
                        break;
                    }
                    var found = existing.GetComponentsInChildren<Transform>(true).FirstOrDefault(t => t.name == compName);
                    if (found != null) {
                        compObj = found.gameObject;
                        wasPreExisting = true;
                        break;
                    }
                }

                // If not found in existing meshes, and it HAS a mesh file, create a new container
                if (compObj == null && !string.IsNullOrEmpty(meshFile)) 
                {
                    compObj = new GameObject(compName);
                } 
                // If still not found and has NO mesh (virtual node), create it only as a last resort
                else if (compObj == null)
                {
                    compObj = new GameObject(compName);
                }

                components[compName] = compObj;
                parentMap[compName] = parentName;

                // Debug.Log($"[AASKitImporter] 🔎 Component '{compName}': Pos=({pos.x}, {pos.y}, {pos.z}), Rot=({rot.x}, {rot.y}, {rot.z}), Scale=({scale.x}, {scale.y}, {scale.z}), wasPreExisting={wasPreExisting}");

                if (!wasPreExisting)
                {
                    // AAS layout is already Y-up (matching Python preview).
                    // Add 180 degrees around Y to compensate for the right-to-left handed coordinate conversion of the glTF importer.
                    compObj.transform.localPosition = new Vector3(pos.x, pos.y, -pos.z);
                    compObj.transform.localEulerAngles = new Vector3(rot.x, rot.y + 180f, rot.z);
                    compObj.transform.localScale = new Vector3(scale.x, scale.y, scale.z);
                    // Debug.Log($"[AASKitImporter] 📐 Applied transform to '{compName}': localPosition={compObj.transform.localPosition}, localEulerAngles={compObj.transform.localEulerAngles}");
                }
                else 
                {
                    // Debug.Log($"[AASKitImporter] 🧊 Found '{compName}' inside GLB. Preserving Blender transform and hierarchy.");
                }

                if (!string.IsNullOrEmpty(meshFile) && !wasPreExisting)
                {
                    string localPath = ResolveMeshPath(meshFile, kitPath);
                    if (!string.IsNullOrEmpty(localPath))
                    {
                        if (localPath.ToLower().EndsWith(".glb") || localPath.ToLower().EndsWith(".gltf"))
                        {
                            var gltf = new GltfImport();
                            string uri = "file:///" + localPath.Replace("\\", "/");
                            bool success = await gltf.Load(uri);
                            if (success) 
                            {
                                await gltf.InstantiateMainSceneAsync(compObj.transform);
                                
                                // Re-apply the transform after GLB instantiation to ensure it is not overwritten or reset by gltfast.
                                // Add 180 degrees around Y to compensate for the glTF importer rotation.
                                compObj.transform.localPosition = new Vector3(pos.x, pos.y, -pos.z);
                                compObj.transform.localEulerAngles = new Vector3(rot.x, rot.y + 180f, rot.z);
                                compObj.transform.localScale = new Vector3(scale.x, scale.y, scale.z);
                                
                                Debug.Log($"[AASKitImporter] 📂 GLB Loaded for '{compName}'. Re-applied localEulerAngles={compObj.transform.localEulerAngles}, Child count={compObj.transform.childCount}");
                                
                                // POST-IMPORT FIXES (UVs, Shaders and Colliders)
                                // Cache source glTF material properties (colors, textures, roughness, metallic)
                                // Because in Standalone builds, glTFast's shader graph may fail to link, causing mat.HasProperty() to return false!
                                Dictionary<string, Material> createdLitMaterials = new Dictionary<string, Material>();
                                Shader urpLitShader = Shader.Find("Universal Render Pipeline/Lit");
                                if (urpLitShader == null) urpLitShader = Shader.Find("Universal Render Pipeline/Simple Lit");
                                if (urpLitShader == null) urpLitShader = Shader.Find("Standard");

                                for (int smIdx = 0; smIdx < gltf.MaterialCount; smIdx++)
                                {
                                    var srcMat = gltf.GetSourceMaterial(smIdx);
                                    if (srcMat == null) continue;

                                    string matKey = !string.IsNullOrEmpty(srcMat.name) ? srcMat.name : $"Material_{smIdx}";
                                    if (urpLitShader != null)
                                    {
                                        Material newMat = new Material(urpLitShader);
                                        newMat.name = matKey;

                                        Color baseCol = Color.white;
                                        float metallic = 0f;
                                        float roughness = 0.5f;
                                        Texture2D mainTexture = null;

                                        if (srcMat.PbrMetallicRoughness != null)
                                        {
                                            baseCol = srcMat.PbrMetallicRoughness.BaseColor;
                                            metallic = srcMat.PbrMetallicRoughness.metallicFactor;
                                            roughness = srcMat.PbrMetallicRoughness.roughnessFactor;

                                            if (srcMat.PbrMetallicRoughness.BaseColorTexture != null &&
                                                srcMat.PbrMetallicRoughness.BaseColorTexture.index >= 0)
                                            {
                                                mainTexture = gltf.GetTexture(srcMat.PbrMetallicRoughness.BaseColorTexture.index);
                                            }
                                        }
                                        else if (srcMat.Extensions?.KHR_materials_pbrSpecularGlossiness != null)
                                        {
                                            baseCol = srcMat.Extensions.KHR_materials_pbrSpecularGlossiness.DiffuseColor;
                                            if (srcMat.Extensions.KHR_materials_pbrSpecularGlossiness.diffuseTexture != null &&
                                                srcMat.Extensions.KHR_materials_pbrSpecularGlossiness.diffuseTexture.index >= 0)
                                            {
                                                mainTexture = gltf.GetTexture(srcMat.Extensions.KHR_materials_pbrSpecularGlossiness.diffuseTexture.index);
                                            }
                                        }

                                        // Apply color (convert linear color if needed, or direct)
                                        if (newMat.HasProperty("_BaseColor")) newMat.SetColor("_BaseColor", baseCol);
                                        if (newMat.HasProperty("_Color")) newMat.color = baseCol;

                                        if (mainTexture != null)
                                        {
                                            if (newMat.HasProperty("_BaseMap")) newMat.SetTexture("_BaseMap", mainTexture);
                                            if (newMat.HasProperty("_MainTex")) newMat.SetTexture("_MainTex", mainTexture);
                                            newMat.mainTexture = mainTexture;
                                        }

                                        if (newMat.HasProperty("_Metallic")) newMat.SetFloat("_Metallic", metallic);
                                        if (newMat.HasProperty("_Smoothness")) newMat.SetFloat("_Smoothness", 1f - roughness);

                                        // Double sided
                                        if (newMat.HasProperty("_Cull")) newMat.SetFloat("_Cull", (float)UnityEngine.Rendering.CullMode.Off);
                                        if (newMat.HasProperty("_CullMode")) newMat.SetFloat("_CullMode", (float)UnityEngine.Rendering.CullMode.Off);

                                        createdLitMaterials[matKey] = newMat;
                                        // Also save by index as fallback
                                        createdLitMaterials[$"Index_{smIdx}"] = newMat;
                                    }
                                }

                                // Map glTF node names to their primitive material keys
                                Dictionary<string, List<string>> nodePrimitiveMatKeys = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
                                var sourceRoot = gltf.GetSourceRoot();
                                if (sourceRoot?.nodes != null && sourceRoot.meshes != null)
                                {
                                    foreach (var node in sourceRoot.nodes)
                                    {
                                        if (string.IsNullOrEmpty(node.name) || node.mesh < 0 || node.mesh >= sourceRoot.meshes.Length) continue;
                                        var mesh = sourceRoot.meshes[node.mesh];
                                        if (mesh?.primitives == null) continue;

                                        List<string> pKeys = new List<string>();
                                        foreach (var prim in mesh.primitives)
                                        {
                                            if (prim.material >= 0 && prim.material < gltf.MaterialCount)
                                            {
                                                var sm = gltf.GetSourceMaterial(prim.material);
                                                string key = (sm != null && !string.IsNullOrEmpty(sm.name)) ? sm.name : $"Material_{prim.material}";
                                                pKeys.Add(key);
                                            }
                                            else
                                            {
                                                pKeys.Add(null);
                                            }
                                        }
                                        nodePrimitiveMatKeys[node.name] = pKeys;
                                    }
                                }

                                foreach (var renderer in compObj.GetComponentsInChildren<MeshRenderer>())
                                {
                                    // 0. Build Standalone Missing Shader Fix (Pink/Magenta fallback & Culling)
                                    Material[] mats = renderer.materials;
                                    bool matChanged = false;
                                    List<string> expectedMatKeys = null;
                                    if (nodePrimitiveMatKeys.ContainsKey(renderer.name))
                                        expectedMatKeys = nodePrimitiveMatKeys[renderer.name];
                                    else if (renderer.transform.parent != null && nodePrimitiveMatKeys.ContainsKey(renderer.transform.parent.name))
                                        expectedMatKeys = nodePrimitiveMatKeys[renderer.transform.parent.name];

                                    for (int m = 0; m < mats.Length; m++)
                                    {
                                        var mat = mats[m];
                                        if (mat == null) continue;

                                        bool isBrokenShader = mat.shader == null || 
                                                              mat.shader.name == "Hidden/InternalErrorShader" ||
                                                              string.IsNullOrEmpty(mat.shader.name);

                                        // If broken shader, restore from source glTF material mapping!
                                        if (isBrokenShader)
                                        {
                                            string cleanMatName = mat.name.Replace(" (Instance)", "").Trim();
                                            Material replacementMat = null;

                                            // 1. Try glTF node primitive mapping (the exact material assigned to this mesh primitive)
                                            if (expectedMatKeys != null && m < expectedMatKeys.Count && !string.IsNullOrEmpty(expectedMatKeys[m]))
                                            {
                                                string targetKey = expectedMatKeys[m];
                                                if (createdLitMaterials.ContainsKey(targetKey))
                                                    replacementMat = createdLitMaterials[targetKey];
                                            }

                                            // 2. Try match by clean material name
                                            if (replacementMat == null && createdLitMaterials.ContainsKey(cleanMatName))
                                                replacementMat = createdLitMaterials[cleanMatName];

                                            // 3. Fallback: match by index only if material count matches slot count
                                            if (replacementMat == null && createdLitMaterials.ContainsKey($"Index_{m}") && mats.Length > 1)
                                                replacementMat = createdLitMaterials[$"Index_{m}"];

                                            if (replacementMat != null)
                                            {
                                                mats[m] = replacementMat;
                                                mat = replacementMat;
                                                matChanged = true;
                                                Debug.LogWarning($"[AASKitImporter] 🛠️ Replaced broken shader in '{renderer.name}' (slot {m}) with rebuilt URP Lit material '{replacementMat.name}' (Color: {replacementMat.color})");
                                            }
                                            else if (urpLitShader != null)
                                            {
                                                // Last resort fallback
                                                mat.shader = urpLitShader;
                                                matChanged = true;
                                            }
                                        }

                                        // FIX INSIDE-OUT EFFECT: Set Cull Mode to Double-Sided (Off = 0)
                                        if (mat.HasProperty("_Cull"))
                                        {
                                            mat.SetFloat("_Cull", (float)UnityEngine.Rendering.CullMode.Off);
                                            matChanged = true;
                                        }
                                        if (mat.HasProperty("_CullMode"))
                                        {
                                            mat.SetFloat("_CullMode", (float)UnityEngine.Rendering.CullMode.Off);
                                            matChanged = true;
                                        }
                                    }
                                    if (matChanged) renderer.materials = mats;

                                    // 1. UV Rotation Fix
                                    bool isBelt = renderer.name.ToLower().Contains("belt") || 
                                                  renderer.materials.Any(m => m.name.ToLower().Contains("belt"));
                                    if (isBelt)
                                    {
                                        MeshFilter mf = renderer.GetComponent<MeshFilter>();
                                        if (mf != null && mf.sharedMesh != null)
                                        {
                                            Debug.Log($"[AASKitImporter] 🔄 Rotating UVs for belt: {renderer.name}");
                                            RotateMeshUVs(mf.sharedMesh);
                                        }
                                    }

                                    // 2. Automatic Colliders
                                    MeshFilter filter = renderer.GetComponent<MeshFilter>();
                                    if (filter != null && filter.sharedMesh != null)
                                    {
                                        MeshCollider mc = renderer.gameObject.AddComponent<MeshCollider>();
                                        // A MeshCollider MUST be convex to collide with other Rigidbodies (like our spawned cube)
                                        mc.convex = true;
                                    }
                                }
                                
                                // Rigidbody for stability - initialized as kinematic to prevent premature falling during import
                                Rigidbody rb = compObj.AddComponent<Rigidbody>();
                                rb.isKinematic = true;

                                Debug.Log($"[AASKitImporter] Successfully loaded GLB: {localPath}");
                            }
                            else Debug.LogWarning($"[AASKitImporter] glTFast failed to load: {uri}");
                        }
                    }
                }
            }

            // 2. Second Pass: Set Hierarchy
            foreach (var kvp in parentMap)
            {
                GameObject child = components[kvp.Key];
                Transform targetParent = null;

                if (!string.IsNullOrEmpty(kvp.Value) && components.ContainsKey(kvp.Value))
                    targetParent = components[kvp.Value].transform;
                else
                    targetParent = kitRoot.transform;

                // LOGIC UPDATE: If the child is already a sub-object from Blender
                // and its CURRENT parent is also part of this kit, we DON'T re-parent.
                // This preserves functional hierarchies (like Drill being child of a Move axis).
                bool currentParentIsKitPart = child.transform.parent != null && 
                                              components.Values.Any(c => c.transform == child.transform.parent);
                
                // NEW: Also check if it's already a descendant of the target parent (deep hierarchy)
                bool isAlreadyDescendant = child.transform.IsChildOf(targetParent);

                if (child.transform.parent != targetParent && !currentParentIsKitPart && !isAlreadyDescendant)
                {
                    child.transform.SetParent(targetParent, true);
                    // Debug.Log($"[AASKitImporter] 🔗 SetParent for {kvp.Key} to {targetParent.name} (New Root/Manual Parent)");
                }
                else if (isAlreadyDescendant && child.transform.parent != targetParent)
                {
                    // Debug.Log($"[AASKitImporter] 🧬 Preserving nested Blender hierarchy for {kvp.Key} (already descendant of {targetParent.name})");
                }
                else if (currentParentIsKitPart && child.transform.parent != targetParent)
                {
                    // Debug.Log($"[AASKitImporter] 🧬 Preserving functional Blender hierarchy for {kvp.Key} (Parent: {child.transform.parent.name})");
                }
            }

            // FINAL STEP: Normalize the entire kit hierarchy.
            // This bakes all scales into meshes and resets all transforms to (1,1,1).
            // This is CRITICAL to prevent rotation shearing (skewing) in Unity.
            NormalizeHierarchy(kitRoot.transform);

            // 3. Third Pass: Activate physics for dynamic products AFTER everything is assembled and normalized
            foreach (var kvp in components)
            {
                string compName = kvp.Key;
                GameObject compObj = kvp.Value;
                if (compObj == null) continue;

                if (dynamicComponents.Contains(compName))
                {
                    Rigidbody rb = compObj.GetComponent<Rigidbody>();
                    if (rb == null) {
                        rb = compObj.AddComponent<Rigidbody>();
                        rb.isKinematic = true;
                    }
                    rb.isKinematic = false;
                    rb.collisionDetectionMode = CollisionDetectionMode.Continuous;
                    rb.constraints = RigidbodyConstraints.FreezeRotationX | RigidbodyConstraints.FreezeRotationZ;
                    
                    // Assign No Friction material to all colliders of this dynamic product to prevent getting stuck on vertical seam steps
                    foreach (var col in compObj.GetComponentsInChildren<Collider>())
                    {
                        col.sharedMaterial = NoFrictionMaterial;
                    }
                    
                    // Add marker component to identify as dynamic product
                    compObj.AddComponent<AAS.DynamicProduct>();
                    
                    Debug.Log($"[AASKitImporter] 📦 Activated Dynamic Product physics for '{compName}'.");
                }
            }

            return new KitImportData { root = kitRoot, components = components };
        } catch (Exception e) {
            Debug.LogError($"[AASKitImporter] Fatal error parsing kit: {e}");
            return null;
        }
    }

    private static void NormalizeHierarchy(Transform root)
    {
        if (root == null) return;

        // Process this node
        Vector3 localScale = root.localScale;
        
        // Debug.Log($"[AASKitImporter] 🧼 NormalizeHierarchy checking '{root.name}': localPosition={root.localPosition}, localEulerAngles={root.localEulerAngles}, localScale={localScale}");

        // 1. Bake any mesh on THIS specific node using its local scale
        BakeMeshesOnObject(root.gameObject, localScale);

        // 2. If this node has scale, we must reset it and fix children
        if (Vector3.Distance(localScale, Vector3.one) > 0.0001f)
        {
            // Capture children
            List<Transform> children = new List<Transform>();
            for (int i = 0; i < root.childCount; i++) children.Add(root.GetChild(i));

            // Detach children to world space (Unity preserves their world transform)
            foreach (var c in children) c.SetParent(null, true);
            
            // Reset parent scale
            root.localScale = Vector3.one;
            
            // Re-attach children (Unity calculates new local scales/positions)
            foreach (var c in children) c.SetParent(root, true);
            
            // Debug.Log($"[AASKitImporter] 🧼 Normalized scale for node: {root.name}. Post-normalization localEulerAngles={root.localEulerAngles}");
        }

        // 3. Recurse to children (they might now have new local scales inherited from step 2)
        // We use a copy of the child list to avoid issues with hierarchy changes
        List<Transform> next = new List<Transform>();
        for (int i = 0; i < root.childCount; i++) next.Add(root.GetChild(i));
        foreach (var n in next) NormalizeHierarchy(n);
    }

    private static void BakeMeshesOnObject(GameObject obj, Vector3 scale)
    {
        if (Vector3.Distance(scale, Vector3.one) <= 0.0001f) return;

        // Handle standard MeshFilters
        foreach (var mf in obj.GetComponents<MeshFilter>())
        {
            if (mf.sharedMesh != null)
            {
                mf.sharedMesh = BakeMeshInstance(mf.sharedMesh, scale);
                // Sync MeshColliders
                foreach (var mc in obj.GetComponents<MeshCollider>())
                {
                    if (mc.sharedMesh != null) mc.sharedMesh = mf.sharedMesh;
                }
            }
        }

        // Handle SkinnedMeshRenderers (sometimes used in GLBs)
        foreach (var smr in obj.GetComponents<SkinnedMeshRenderer>())
        {
            if (smr.sharedMesh != null)
            {
                smr.sharedMesh = BakeMeshInstance(smr.sharedMesh, scale);
            }
        }
    }

    private static Mesh BakeMeshInstance(Mesh original, Vector3 scale)
    {
        Mesh mesh = Instantiate(original);
        Vector3[] v = mesh.vertices;
        for (int i = 0; i < v.Length; i++)
        {
            v[i] = Vector3.Scale(v[i], scale);
        }
        mesh.vertices = v;

        // If any axis scale is flipped (negative determinant), winding order of triangles gets reversed.
        // Inverting the triangles restores the correct outward-facing normals.
        bool hasNegativeScale = (scale.x * scale.y * scale.z) < 0f;
        if (hasNegativeScale)
        {
            for (int s = 0; s < mesh.subMeshCount; s++)
            {
                int[] tris = mesh.GetTriangles(s);
                for (int t = 0; t < tris.Length; t += 3)
                {
                    int temp = tris[t];
                    tris[t] = tris[t + 2];
                    tris[t + 2] = temp;
                }
                mesh.SetTriangles(tris, s);
            }
        }

        mesh.RecalculateBounds();
        mesh.RecalculateNormals();
        mesh.RecalculateTangents();
        return mesh;
    }

    private static void RotateMeshUVs(Mesh mesh)
    {
        Vector2[] uvs = mesh.uv;
        for (int i = 0; i < uvs.Length; i++)
        {
            float oldX = uvs[i].x;
            uvs[i].x = uvs[i].y;
            uvs[i].y = 1.0f - oldX;
        }
        mesh.uv = uvs;
    }

    private static string ResolveMeshPath(string filename, string rootDir)
    {
        string clean = filename.Split(new[] { '/', '\\', '-' }).Last();
        if (System.IO.Directory.Exists(rootDir))
        {
            string[] allFiles = System.IO.Directory.GetFiles(rootDir, clean, System.IO.SearchOption.AllDirectories);
            if (allFiles.Length > 0) return allFiles[0];
        }
        return null;
    }

    private static float SafeFloat(Newtonsoft.Json.Linq.JToken v)
    {
        if (v == null) return 0f;
        if (v.Type == Newtonsoft.Json.Linq.JTokenType.Float || v.Type == Newtonsoft.Json.Linq.JTokenType.Integer)
            return (float)v;
        string s = v.ToString().Replace(',', '.');
        if (float.TryParse(s, System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out float res))
            return res;
        return 0f;
    }

    private static PhysicsMaterial _noFrictionMaterial;
    private static PhysicsMaterial NoFrictionMaterial {
        get {
            if (_noFrictionMaterial == null) {
                _noFrictionMaterial = new PhysicsMaterial("NoFrictionProduct") {
                    dynamicFriction = 0f,
                    staticFriction = 0f,
                    frictionCombine = PhysicsMaterialCombine.Minimum,
                    bounceCombine = PhysicsMaterialCombine.Minimum
                };
            }
            return _noFrictionMaterial;
        }
    }
}
