using UnityEngine;
using UnityEngine.UI;
using TMPro;

/// <summary>
/// Attach this script to a UI Panel or Canvas in the Unity Editor.
/// Connect your TextMeshProUGUI labels and Button in the Inspector.
/// </summary>
public class ModeSelectorUI : MonoBehaviour
{
    [Header("UI References")]
    [Tooltip("Text showing the active mode, e.g., 'Currently on DT'")]
    public TextMeshProUGUI activeModeText;

    [Tooltip("Text inside the toggle button, e.g., 'Change to VC'")]
    public TextMeshProUGUI buttonActionText;

    [Tooltip("Button that toggles between VC and DT modes.")]
    public Button toggleModeButton;

    [Header("Configuration")]
    [Tooltip("The operation mode that the system should start in.")]
    public OperationMode initialMode = OperationMode.VirtualCommissioning;

    void Awake()
    {
        OperationModeManager.SetMode(initialMode);
    }

    void Start()
    {
        if (toggleModeButton != null)
        {
            toggleModeButton.onClick.AddListener(OnToggleButtonClicked);
        }

        // Subscribe to global operation mode changes
        OperationModeManager.OnModeChanged += RefreshUI;

        // Render initial state
        RefreshUI(OperationModeManager.Current);
    }

    void OnDestroy()
    {
        OperationModeManager.OnModeChanged -= RefreshUI;
    }

    private void OnToggleButtonClicked()
    {
        // Toggle the global operation mode
        if (OperationModeManager.Current == OperationMode.VirtualCommissioning)
        {
            OperationModeManager.SetMode(OperationMode.DigitalTwin);
        }
        else
        {
            OperationModeManager.SetMode(OperationMode.VirtualCommissioning);
        }
    }

    private void RefreshUI(OperationMode mode)
    {
        if (activeModeText == null || buttonActionText == null) return;

        if (mode == OperationMode.DigitalTwin)
        {
            activeModeText.text = "Currently on DT";
            buttonActionText.text = "Change to VC";
        }
        else
        {
            activeModeText.text = "Currently on VC";
            buttonActionText.text = "Change to DT";
        }
    }
}
