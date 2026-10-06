using UnityEngine;

public class AASVisualJoint : MonoBehaviour
{
    public string jointName;
    public Vector3 axis;
    public string jointType;
    
    private Quaternion initialRotation;
    private float currentAngle;

    void Awake()
    {
        initialRotation = transform.localRotation;
    }

    public void SetAngle(float angle)
    {
        currentAngle = angle;
        // Apply rotation around the URDF axis
        transform.localRotation = initialRotation * Quaternion.AngleAxis(currentAngle, axis);
    }
}
