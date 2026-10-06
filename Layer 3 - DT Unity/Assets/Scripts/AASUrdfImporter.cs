using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Xml.Linq;
using UnityEngine;
using Unity.Robotics.UrdfImporter;

public class AASUrdfImporter : MonoBehaviour
{
    public struct JointData
    {
        public string name;
        public GameObject anchor;
        public Vector3 axis;
        public string type;
    }

    public class ImportResult
    {
        public GameObject root;
        public List<JointData> joints = new List<JointData>();
    }

    public static ImportResult Import(string urdfPath, string robotName, Transform parent = null)
    {
        if (!File.Exists(urdfPath)) return null;

        XDocument doc = XDocument.Load(urdfPath);
        XElement root = doc.Element("robot");

        // Detect if any visual geometry uses a .dae (Collada) mesh
        bool hasDaeMeshes = false;
        if (root != null)
        {
            foreach (var mesh in root.Descendants("mesh"))
            {
                string filename = mesh.Attribute("filename")?.Value;
                if (filename != null && filename.ToLower().EndsWith(".dae"))
                {
                    hasDaeMeshes = true;
                    break;
                }
            }
        }

        GameObject robotRoot = new GameObject(robotName);
        if (parent != null) robotRoot.transform.SetParent(parent, false);

        var result = new ImportResult { root = robotRoot };
        
        // Namespace-independent parsing
        var links = root.Elements().Where(e => e.Name.LocalName.Equals("link", StringComparison.OrdinalIgnoreCase))
                                   .ToDictionary(l => l.Attribute("name").Value);
        var joints = root.Elements().Where(e => e.Name.LocalName.Equals("joint", StringComparison.OrdinalIgnoreCase))
                                    .ToList();

        // Find root link in a namespace-independent way
        var childLinks = joints.Select(j => j.Elements().FirstOrDefault(e => e.Name.LocalName.Equals("child", StringComparison.OrdinalIgnoreCase))?.Attribute("link")?.Value).Where(v => v != null).ToHashSet();
        var rootLinkName = links.Keys.FirstOrDefault(name => !childLinks.Contains(name));
        if (string.IsNullOrEmpty(rootLinkName)) rootLinkName = links.Keys.First();

        Debug.Log($"[AAS-URDF] Importing robot: name={robotName}, hasDaeMeshes={hasDaeMeshes}, rootLink={rootLinkName}");

        // Set Runtime Mode for the importer to handle paths correctly without AssetDatabase
        RuntimeUrdf.SetRuntimeMode(true);

        CreateLinkRecursive(rootLinkName, links, joints, robotRoot.transform, urdfPath, result);

        return result;
    }

    private static void CreateLinkRecursive(string linkName, Dictionary<string, XElement> links, List<XElement> joints, Transform parent, string urdfPath, ImportResult result)
    {
        GameObject linkObj = new GameObject(linkName);
        linkObj.transform.SetParent(parent, false);
        linkObj.transform.localPosition = Vector3.zero;
        linkObj.transform.localRotation = Quaternion.identity;

        XElement linkXml = links[linkName];

        // Create Visuals (namespace-independent)
        foreach (var visualXml in linkXml.Elements().Where(e => e.Name.LocalName.Equals("visual", StringComparison.OrdinalIgnoreCase)))
        {
            CreateVisual(visualXml, linkObj.transform, urdfPath);
        }

        // Find child joints (namespace-independent)
        var childJoints = joints.Where(j => {
            var parentEl = j.Elements().FirstOrDefault(e => e.Name.LocalName.Equals("parent", StringComparison.OrdinalIgnoreCase));
            return parentEl != null && parentEl.Attribute("link")?.Value == linkName;
        }).ToList();

        foreach (var joint in childJoints)
        {
            string childName = joint.Elements().FirstOrDefault(e => e.Name.LocalName.Equals("child", StringComparison.OrdinalIgnoreCase))?.Attribute("link")?.Value;
            string jointName = joint.Attribute("name")?.Value;
            string jointType = joint.Attribute("type")?.Value ?? "fixed";
            
            var axisEl = joint.Elements().FirstOrDefault(e => e.Name.LocalName.Equals("axis", StringComparison.OrdinalIgnoreCase));
            Vector3 axis = ParseVector3(axisEl?.Attribute("xyz")?.Value, Vector3.right);

            // Create Joint Anchor (handles origin)
            GameObject jointAnchor = new GameObject("Joint_" + jointName);
            jointAnchor.transform.SetParent(linkObj.transform, false);

            XElement origin = joint.Elements().FirstOrDefault(e => e.Name.LocalName.Equals("origin", StringComparison.OrdinalIgnoreCase));
            if (origin != null)
            {
                Vector3 pos = ParseVector3(origin.Attribute("xyz")?.Value, Vector3.zero);
                Vector3 rpy = ParseVector3(origin.Attribute("rpy")?.Value, Vector3.zero);
                
                // EXACT parity with UrdfOrigin.ImportOriginData
                jointAnchor.transform.Translate(new Vector3(-pos.y, pos.z, pos.x));
                jointAnchor.transform.Rotate(new Vector3(rpy.y * Mathf.Rad2Deg, -rpy.z * Mathf.Rad2Deg, -rpy.x * Mathf.Rad2Deg));
                
                Debug.Log($"[AAS-URDF] Joint '{jointName}' (Link {linkName} -> {childName}): Pos={jointAnchor.transform.localPosition}, Rot={jointAnchor.transform.localEulerAngles}");
            }

            // Store Joint Data for Sync
            result.joints.Add(new JointData {
                name = jointName,
                anchor = jointAnchor,
                // Axis also uses position mapping: (-y, z, x)
                axis = new Vector3(-axis.y, axis.z, axis.x).normalized, 
                type = jointType
            });

            CreateLinkRecursive(childName, links, joints, jointAnchor.transform, urdfPath, result);
        }
    }

    private static void CreateVisual(XElement visualXml, Transform parent, string urdfPath)
    {
        string visName = visualXml.Attribute("name")?.Value ?? "Visual";
        
        // The Origin Anchor handles the transform defined in <origin>
        GameObject visOriginObj = new GameObject(visName);
        visOriginObj.transform.SetParent(parent, false);

        XElement origin = visualXml.Elements().FirstOrDefault(e => e.Name.LocalName.Equals("origin", StringComparison.OrdinalIgnoreCase));
        if (origin != null)
        {
            Vector3 pos = ParseVector3(origin.Attribute("xyz")?.Value, Vector3.zero);
            Vector3 rpy = ParseVector3(origin.Attribute("rpy")?.Value, Vector3.zero);
            
            // EXACT parity with UrdfOrigin.ImportOriginData
            visOriginObj.transform.Translate(new Vector3(-pos.y, pos.z, pos.x));
            visOriginObj.transform.Rotate(new Vector3(rpy.y * Mathf.Rad2Deg, -rpy.z * Mathf.Rad2Deg, -rpy.x * Mathf.Rad2Deg));
            
            Debug.Log($"   [Visual: {visName}] Pos={visOriginObj.transform.localPosition}, Rot={visOriginObj.transform.localEulerAngles}");
        }

        // The Geometry Anchor receives the mesh and handles the scale
        GameObject visGeometryObj = new GameObject("Geometry");
        visGeometryObj.transform.SetParent(visOriginObj.transform, false);

        XElement geometryXml = visualXml.Elements().FirstOrDefault(e => e.Name.LocalName.Equals("geometry", StringComparison.OrdinalIgnoreCase));
        if (geometryXml != null)
        {
            try {
                Link.Geometry geometry = new Link.Geometry(geometryXml);
                if (geometry.mesh != null) geometry.mesh.filename = ResolveMeshPath(geometry.mesh.filename, urdfPath);

                GeometryTypes type = UrdfGeometry.GetGeometryType(geometry);
                
                // UrdfGeometryVisual.Create calls SetScale on its PARENT. 
                // We MUST pass visGeometryObj so that the scale doesn't distort the origin's Translate vector.
                UrdfGeometryVisual.Create(visGeometryObj.transform, type, geometry);
            }
            catch (Exception e) {
                Debug.LogWarning($"[AAS-URDF] Failed to create visual: {e.Message}");
            }
        }
    }

    // Using official extension methods directly in logic now.

    private static Vector3 ParseVector3(string value, Vector3 defaultVal)
    {
        if (string.IsNullOrEmpty(value)) return defaultVal;
        try {
            var parts = value.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries)
                             .Select(s => float.Parse(s, System.Globalization.CultureInfo.InvariantCulture))
                             .ToArray();
            return parts.Length >= 3 ? new Vector3(parts[0], parts[1], parts[2]) : defaultVal;
        } catch { return defaultVal; }
    }

    private static string ResolveMeshPath(string filename, string urdfPath)
    {
        string clean = filename.Replace("package://", "").Replace("\\", "/");
        string rootDir = Path.GetDirectoryName(urdfPath);
        string fileNameOnly = Path.GetFileName(clean);
        
        // Strategy 1: Direct path check (normalized)
        string directPath = Path.Combine(rootDir, clean);
        if (File.Exists(directPath)) return "file://" + directPath.Replace("\\", "/");
        
        // Strategy 2: Upward search for package root
        string currentDir = rootDir;
        for (int i = 0; i < 3; i++) // Search up to 3 levels up
        {
            string candidate = Path.Combine(currentDir, clean);
            if (File.Exists(candidate)) return "file://" + candidate.Replace("\\", "/");
            currentDir = Path.GetDirectoryName(currentDir);
            if (string.IsNullOrEmpty(currentDir)) break;
        }

        // Strategy 3: Failsafe fuzzy search WITHIN robot folder
        string[] allFiles = Directory.GetFiles(rootDir, fileNameOnly, SearchOption.AllDirectories);
        string bestMatch = null;
        int maxTailMatch = -1;

        foreach (var file in allFiles)
        {
            string normalizedFile = file.Replace("\\", "/");
            // Check how much of the original path matches the end of the found file
            string[] parts = clean.Split('/');
            int matchCount = 0;
            for (int j = 1; j <= parts.Length; j++)
            {
                string tail = string.Join("/", parts.Skip(parts.Length - j));
                if (normalizedFile.EndsWith(tail, StringComparison.OrdinalIgnoreCase)) matchCount = j;
                else break;
            }

            if (matchCount > maxTailMatch)
            {
                maxTailMatch = matchCount;
                bestMatch = normalizedFile;
            }
        }

        if (bestMatch != null) return "file://" + bestMatch;
        
        return "file://" + Path.Combine(rootDir, clean).Replace("\\", "/");
    }
}
