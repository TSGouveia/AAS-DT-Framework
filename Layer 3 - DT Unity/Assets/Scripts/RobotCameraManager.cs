using UnityEngine;
using System.Collections.Generic;
using System.Linq;

#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem;
#endif

public class RobotCameraManager : MonoBehaviour
{
    public static RobotCameraManager Instance { get; private set; }

    [Header("Targeting & Focus")]
    public List<Transform> targets = new List<Transform>();
    public float focusDistanceMultiplier = 2.0f;
    public float focusSmoothSpeed = 5f;

    [Header("Movement Settings")]
    public float moveSpeed = 10f;
    public float fastMoveMultiplier = 2.5f;
    public float lookSensitivity = 0.15f;
    public float panSpeed = 0.05f;
    public float zoomSensitivity = 1.5f;

    private float yaw = 0f;
    private float pitch = 0f;
    private bool rotationInitialized = false;

    private Camera mainCam;
    private Vector3 targetFocusPosition;
    private bool isTransitioningToFocus = false;

    void Awake()
    {
        if (Instance == null) Instance = this;
        else { Destroy(gameObject); return; }

        mainCam = Camera.main;
    }

    void Start()
    {
        InitializeRotation();
    }

    private void InitializeRotation()
    {
        Transform camTransform = mainCam != null ? mainCam.transform : transform;
        if (camTransform != null)
        {
            Vector3 angles = camTransform.eulerAngles;
            yaw = angles.y;
            pitch = angles.x;
            if (pitch > 180f) pitch -= 360f;
            rotationInitialized = true;
        }
    }

    void LateUpdate()
    {
        Transform camTransform = mainCam != null ? mainCam.transform : transform;
        if (camTransform == null) return;

        if (!rotationInitialized)
        {
            InitializeRotation();
        }

        // --- 1. ZOOM (Scroll Wheel) ---
        float scrollInput = GetScrollValue();
        if (Mathf.Abs(scrollInput) > 0.001f)
        {
            isTransitioningToFocus = false;
            float speedMult = IsShiftPressed() ? fastMoveMultiplier : 1.0f;
            camTransform.Translate(Vector3.forward * scrollInput * zoomSensitivity * speedMult * 5f, Space.Self);
        }

        // --- 2. ROTATION & WASD FLY (RMB held) ---
        if (IsRightClickHeld())
        {
            isTransitioningToFocus = false;

            // Cursor behavior: Hide and lock cursor during flythrough
            Cursor.lockState = CursorLockMode.Locked;
            Cursor.visible = false;

            // Rotate camera
            Vector2 mouseDelta = GetMouseDelta();
            yaw += mouseDelta.x * lookSensitivity * 10f;
            pitch -= mouseDelta.y * lookSensitivity * 10f;
            pitch = Mathf.Clamp(pitch, -85f, 85f);
            
            camTransform.rotation = Quaternion.Euler(pitch, yaw, 0f);

            // Translate camera (WASD / QE)
            Vector3 inputDir = GetInputTranslation();
            if (inputDir.sqrMagnitude > 0.01f)
            {
                float speedMult = IsShiftPressed() ? fastMoveMultiplier : 1.0f;
                Vector3 moveDelta = camTransform.TransformDirection(inputDir) * moveSpeed * speedMult * Time.deltaTime;
                camTransform.position += moveDelta;
            }
        }
        else
        {
            // Reset cursor if RMB is released
            if (Cursor.lockState == CursorLockMode.Locked)
            {
                Cursor.lockState = CursorLockMode.None;
                Cursor.visible = true;
            }
        }

        // --- 3. PAN (MMB held) ---
        if (IsMiddleClickHeld())
        {
            isTransitioningToFocus = false;

            Vector2 mouseDelta = GetMouseDelta();
            if (mouseDelta.sqrMagnitude > 0.001f)
            {
                float speedMult = IsShiftPressed() ? fastMoveMultiplier : 1.0f;
                // Slide camera on its local horizontal/vertical axes
                Vector3 panDelta = new Vector3(-mouseDelta.x, -mouseDelta.y, 0f) * panSpeed * speedMult;
                camTransform.Translate(panDelta, Space.Self);
            }
        }

        // --- 4. FOCUS (Key F) ---
        if (IsFocusKeyPressed())
        {
            FocusOnTargets(camTransform);
        }

        if (isTransitioningToFocus)
        {
            camTransform.position = Vector3.Lerp(camTransform.position, targetFocusPosition, focusSmoothSpeed * Time.deltaTime);
            if (Vector3.Distance(camTransform.position, targetFocusPosition) < 0.05f)
            {
                camTransform.position = targetFocusPosition;
                isTransitioningToFocus = false;
            }
        }
    }

    private void FocusOnTargets(Transform camTransform)
    {
        Vector3 center = Vector3.zero;
        float distance = 10f;

        targets = targets.Where(t => t != null).ToList();
        if (targets.Count > 0)
        {
            Bounds baseBounds = new Bounds(targets[0].position, Vector3.one * 0.1f);
            foreach (var t in targets)
            {
                center += t.position;
                baseBounds.Encapsulate(t.position);
            }
            center /= targets.Count;
            float maxDim = Mathf.Max(baseBounds.size.x, baseBounds.size.z);
            distance = Mathf.Max(5f, maxDim * focusDistanceMultiplier);
        }

        targetFocusPosition = center - camTransform.forward * distance;
        isTransitioningToFocus = true;
    }

    // --- Helper Input Methods to support both Old and New Input Systems ---

    private Vector3 GetInputTranslation()
    {
        Vector3 moveInput = Vector3.zero;

#if ENABLE_INPUT_SYSTEM
        if (Keyboard.current != null)
        {
            var keyboard = Keyboard.current;
            if (keyboard.wKey.isPressed) moveInput.z += 1f;
            if (keyboard.sKey.isPressed) moveInput.z -= 1f;
            if (keyboard.aKey.isPressed) moveInput.x -= 1f;
            if (keyboard.dKey.isPressed) moveInput.x += 1f;
            if (keyboard.eKey.isPressed) moveInput.y += 1f; // E for Up
            if (keyboard.qKey.isPressed) moveInput.y -= 1f; // Q for Down
            return moveInput;
        }
#endif

#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        if (Input.GetKey(KeyCode.W)) moveInput.z += 1f;
        if (Input.GetKey(KeyCode.S)) moveInput.z -= 1f;
        if (Input.GetKey(KeyCode.A)) moveInput.x -= 1f;
        if (Input.GetKey(KeyCode.D)) moveInput.x += 1f;
        if (Input.GetKey(KeyCode.E)) moveInput.y += 1f;
        if (Input.GetKey(KeyCode.Q)) moveInput.y -= 1f;
#endif

        return moveInput;
    }

    private bool IsShiftPressed()
    {
#if ENABLE_INPUT_SYSTEM
        if (Keyboard.current != null)
        {
            return Keyboard.current.leftShiftKey.isPressed || Keyboard.current.rightShiftKey.isPressed;
        }
#endif

#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.LeftShift) || Input.GetKey(KeyCode.RightShift);
#endif
        return false;
    }

    private bool IsFocusKeyPressed()
    {
#if ENABLE_INPUT_SYSTEM
        if (Keyboard.current != null)
        {
            return Keyboard.current.fKey.wasPressedThisFrame;
        }
#endif

#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKeyDown(KeyCode.F);
#endif
        return false;
    }

    private bool IsRightClickHeld()
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null)
        {
            return Mouse.current.rightButton.isPressed;
        }
#endif

#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetMouseButton(1);
#endif
        return false;
    }

    private bool IsMiddleClickHeld()
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null)
        {
            return Mouse.current.middleButton.isPressed;
        }
#endif

#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetMouseButton(2);
#endif
        return false;
    }

    private Vector2 GetMouseDelta()
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null)
        {
            // Delta in pixels, scale it to match legacy axis feel (roughly ~1/20 of pixel values depending on screen/DPI)
            return Mouse.current.delta.ReadValue() * 0.05f;
        }
#endif

#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        return new Vector2(Input.GetAxis("Mouse X"), Input.GetAxis("Mouse Y"));
#endif
        return Vector2.zero;
    }

    private float GetScrollValue()
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null)
        {
            // Scroll y in pixels is typically 120 or -120 per notch. Scale to match legacy axis (-0.1 to 0.1 range)
            return Mouse.current.scroll.ReadValue().y * 0.005f;
        }
#endif

#if !ENABLE_INPUT_SYSTEM || ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetAxis("Mouse ScrollWheel");
#endif
        return 0f;
    }

    // Keep public targeting interface methods for BaSyxManager to compile without modification
    public void AddTarget(Transform target)
    {
        if (target == null) return;
        if (!targets.Contains(target))
        {
            targets.Add(target);
            Debug.Log($"[Robot-Camera] Base registered: {target.name}");
        }
    }

    public void RemoveTarget(Transform target)
    {
        targets.Remove(target);
    }
}
