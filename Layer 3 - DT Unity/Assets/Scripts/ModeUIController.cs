using UnityEngine;
using UnityEngine.UI;

/// <summary>
/// Spawns a minimal overlay UI with two mode buttons:
///   - Virtual Commissioning (VC): physics sensors + rule engine active
///   - Digital Twin (DT):          real hardware sensors via HTTP REST; rule engine paused
///
/// Auto-created at runtime via RuntimeInitializeOnLoadMethod; no manual setup needed.
/// </summary>
public class ModeUIController : MonoBehaviour
{
    private Button _vcButton;
    private Button _dtButton;
    private Text   _vcLabel;
    private Text   _dtLabel;
    private Image  _vcImage;
    private Image  _dtImage;

    // ----- Colour palette -----
    private static readonly Color ColActive     = new Color(0.11f, 0.52f, 0.30f); // green
    private static readonly Color ColInactive   = new Color(0.16f, 0.16f, 0.16f); // dark
    private static readonly Color ColTextActive = Color.white;
    private static readonly Color ColTextOff    = new Color(0.55f, 0.55f, 0.55f);
    private static readonly Color ColPanel      = new Color(0.08f, 0.08f, 0.08f, 0.88f);
    private static readonly Color ColBadgeVC    = new Color(0.20f, 0.72f, 0.45f);
    private static readonly Color ColBadgeDT    = new Color(0.18f, 0.52f, 0.82f);

    // -----------------------------------------------------------------------
    // [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    // static void AutoCreate()
    // {
    //     var go = new GameObject("[ModeUIController]");
    //     go.AddComponent<ModeUIController>();
    //     DontDestroyOnLoad(go);
    // }

    // -----------------------------------------------------------------------
    void Start()
    {
        BuildUI();
        OperationModeManager.OnModeChanged += OnModeChanged;
        Refresh(OperationModeManager.Current);
    }

    void OnDestroy()
    {
        OperationModeManager.OnModeChanged -= OnModeChanged;
    }

    // -----------------------------------------------------------------------
    private void BuildUI()
    {
        // ---- Canvas ----
        var canvasGo = new GameObject("ModeOverlayCanvas");
        canvasGo.transform.SetParent(transform);
        var canvas = canvasGo.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.ScreenSpaceOverlay;
        canvas.sortingOrder = 999;
        var scaler = canvasGo.AddComponent<CanvasScaler>();
        scaler.uiScaleMode       = CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution = new Vector2(1920, 1080);
        scaler.screenMatchMode   = CanvasScaler.ScreenMatchMode.MatchWidthOrHeight;
        scaler.matchWidthOrHeight = 0.5f;
        canvasGo.AddComponent<GraphicRaycaster>();

        // ---- Outer panel ----
        var panel     = CreateGO("ModePanel", canvasGo.transform);
        var panelRect = panel.AddComponent<RectTransform>();
        panelRect.anchorMin      = new Vector2(0.5f, 1f);
        panelRect.anchorMax      = new Vector2(0.5f, 1f);
        panelRect.pivot          = new Vector2(0.5f, 1f);
        panelRect.anchoredPosition = new Vector2(0f, -12f);
        panelRect.sizeDelta      = new Vector2(480f, 52f);
        var panelImg = panel.AddComponent<Image>();
        panelImg.color = ColPanel;

        var layout = panel.AddComponent<HorizontalLayoutGroup>();
        layout.padding          = new RectOffset(8, 8, 8, 8);
        layout.spacing          = 8f;
        layout.childForceExpandWidth  = true;
        layout.childForceExpandHeight = true;
        layout.childAlignment   = TextAnchor.MiddleCenter;

        // ---- Buttons ----
        (_vcButton, _vcImage, _vcLabel) = MakeButton(panel.transform, "\u2699  Virtual Commissioning",  ColBadgeVC);
        (_dtButton, _dtImage, _dtLabel) = MakeButton(panel.transform, "\ud83d\udd17  Digital Twin",              ColBadgeDT);

        _vcButton.onClick.AddListener(() => OperationModeManager.SetMode(OperationMode.VirtualCommissioning));
        _dtButton.onClick.AddListener(() => OperationModeManager.SetMode(OperationMode.DigitalTwin));
    }

    private (Button btn, Image img, Text lbl) MakeButton(Transform parent, string label, Color accentColor)
    {
        var go   = CreateGO(label, parent);
        var img  = go.AddComponent<Image>();
        img.color = ColInactive;
        var btn  = go.AddComponent<Button>();
        btn.targetGraphic = img;
        var cols = btn.colors;
        cols.highlightedColor = new Color(accentColor.r * 0.6f, accentColor.g * 0.6f, accentColor.b * 0.6f);
        cols.pressedColor     = new Color(accentColor.r * 0.4f, accentColor.g * 0.4f, accentColor.b * 0.4f);
        cols.normalColor      = ColInactive;
        btn.colors = cols;

        // border strip (left side)
        var strip     = CreateGO("Strip", go.transform);
        var stripRect = strip.AddComponent<RectTransform>();
        stripRect.anchorMin      = Vector2.zero;
        stripRect.anchorMax      = new Vector2(0f, 1f);
        stripRect.pivot          = new Vector2(0f, 0.5f);
        stripRect.offsetMin      = Vector2.zero;
        stripRect.offsetMax      = new Vector2(4f, 0f);
        var stripImg = strip.AddComponent<Image>();
        stripImg.color = accentColor;

        // label
        var textGo   = CreateGO("Label", go.transform);
        var textRect = textGo.AddComponent<RectTransform>();
        textRect.anchorMin  = Vector2.zero;
        textRect.anchorMax  = Vector2.one;
        textRect.offsetMin  = new Vector2(8f, 0f);
        textRect.offsetMax  = Vector2.zero;
        var text = textGo.AddComponent<Text>();
        text.text      = label;
        text.font      = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        text.fontSize  = 12;
        text.fontStyle = FontStyle.Bold;
        text.alignment = TextAnchor.MiddleCenter;
        text.color     = ColTextOff;

        return (btn, img, text);
    }

    // -----------------------------------------------------------------------
    private void OnModeChanged(OperationMode mode) => Refresh(mode);

    private void Refresh(OperationMode mode)
    {
        bool isVC = mode == OperationMode.VirtualCommissioning;
        Apply(_vcImage, _vcLabel, isVC);
        Apply(_dtImage, _dtLabel, !isVC);
    }

    private static void Apply(Image img, Text lbl, bool active)
    {
        if (img != null) img.color = active ? ColActive   : ColInactive;
        if (lbl != null) lbl.color = active ? ColTextActive : ColTextOff;
    }

    // -----------------------------------------------------------------------
    private static GameObject CreateGO(string name, Transform parent)
    {
        var go = new GameObject(name);
        go.transform.SetParent(parent, false);
        return go;
    }
}
