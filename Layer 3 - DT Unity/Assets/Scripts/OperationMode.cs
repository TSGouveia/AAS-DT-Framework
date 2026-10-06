using System;
using UnityEngine;

/// <summary>
/// Global operation mode for the Digital Twin system.
/// VirtualCommissioning: Unity physics drive sensors; rule engine active.
/// DigitalTwin: Real hardware drives sensors via HTTP REST; rule engine paused;
///              Unity is a pure visualization layer.
/// </summary>
public enum OperationMode
{
    VirtualCommissioning,
    DigitalTwin
}

/// <summary>
/// Singleton-style static manager for the current operation mode.
/// Subscribe to OnModeChanged to react to mode switches at runtime.
/// </summary>
public static class OperationModeManager
{
    public static OperationMode Current { get; private set; } = OperationMode.VirtualCommissioning;

    /// <summary>Fired whenever the mode changes, passing the NEW mode.</summary>
    public static event Action<OperationMode> OnModeChanged;

    public static void SetMode(OperationMode mode)
    {
        if (Current == mode) return;
        Current = mode;
        Debug.Log($"[OperationMode] \u2192 {mode}");
        OnModeChanged?.Invoke(mode);
    }
}
