using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using UnityEngine;
using AAS;

/// <summary>
/// Extrai e exporta metricas de desempenho e ciclo de vida diretamente para um ficheiro CSV.
/// Nao realiza calculos estatisticos complexos; foca-se na extracao fiel de dados brutos.
/// </summary>
public class PerformanceDataLogger : MonoBehaviour
{
    [Header("Configuracao de Saida")]
    [Tooltip("Nome da pasta onde os ficheiros CSV serao guardados (relativo a raiz do projeto)")]
    public string outputFolder = "Metrics_Logs";
    
    [Tooltip("Intervalo em segundos entre cada registo de metricas no CSV")]
    [Range(0.1f, 5.0f)]
    public float samplingInterval = 0.5f;

    [Header("Finalizacao Automatica")]
    [Tooltip("Se ativo, grava o CSV e encerra automaticamente a aplicacao (ou para o Play mode) apos a conclusao do Startup")]
    public bool quitAfterStartup = false;

    [Tooltip("Tempo em segundos de recolha de metricas apos o startup terminar antes de fechar a aplicacao")]
    public float postStartupDuration = 5.0f;

    [Tooltip("Gravar automaticamente ao sair do Play Mode (caso feches manualmente)")]
    public bool autoSaveOnQuit = true;

    [Header("Atalhos")]
    public KeyCode saveKey = KeyCode.F11;

    // Estrutura de dados brutos
    private struct MetricEntry
    {
        public float timeSinceStart;
        public float fps;
        public float frameTimeMs;
        public float ramAllocatedMB;
        public float ramTotalReservedMB;
        public string operationMode;
        public int activeKitsCount;
        public float startupTimeSec;
        public bool isStartupDone;
    }

    private List<MetricEntry> recordedEntries = new List<MetricEntry>(2000);
    private float nextSampleTime = 0f;
    private float startupRecordedTime = -1f;
    private string csvFilePath;

    void Awake()
    {
        // Garante que o separador decimal sera '.' para facilitar a importacao no Excel / Pandas
        CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
        CultureInfo.DefaultThreadCurrentUICulture = CultureInfo.InvariantCulture;

        // No Editor: guarda na raiz do projeto
        // Na Build (Windows/Standalone): guarda na pasta ao lado do ficheiro .exe
        string baseDir = Application.isEditor 
            ? Directory.GetCurrentDirectory() 
            : Directory.GetParent(Application.dataPath).FullName;

        string folderPath = Path.Combine(baseDir, outputFolder);
        if (!Directory.Exists(folderPath))
        {
            Directory.CreateDirectory(folderPath);
        }

        string fileName = $"run_metrics_{DateTime.Now:yyyyMMdd_HHmmss}.csv";
        csvFilePath = Path.Combine(folderPath, fileName);
    }

    private bool isQuittingInitiated = false;

    void Update()
    {
        // Deteta se o BaSyxManager acabou de registar o Startup Complete
        if (startupRecordedTime < 0f && BaSyxManager.IsStartupComplete)
        {
            startupRecordedTime = Time.realtimeSinceStartup;
        }

        // Amostragem periodica
        if (Time.realtimeSinceStartup >= nextSampleTime)
        {
            nextSampleTime = Time.realtimeSinceStartup + samplingInterval;
            RecordSample();
        }

        // Se configurado para fechar apos o startup
        if (quitAfterStartup && !isQuittingInitiated && BaSyxManager.IsStartupComplete)
        {
            if (Time.realtimeSinceStartup >= (startupRecordedTime + postStartupDuration))
            {
                isQuittingInitiated = true;
                CloseApplicationAfterTest();
            }
        }

        // Atalho manual para forcar escrita em disco
        if (Input.GetKeyDown(saveKey))
        {
            SaveToCSV();
        }
    }

    private void CloseApplicationAfterTest()
    {
        Debug.Log("[PerformanceDataLogger] 🏁 Teste concluido! A guardar CSV e a encerrar aplicacao...");
        SaveToCSV();

#if UNITY_EDITOR
        UnityEditor.EditorApplication.isPlaying = false;
#else
        Application.Quit();
#endif
    }

    private void RecordSample()
    {
        float frameTime = Time.unscaledDeltaTime;
        float currentFps = frameTime > 0.0001f ? (1.0f / frameTime) : 0f;
        float frameTimeMs = frameTime * 1000f;

        // Memoria do Unity GC e Profiler (em MB)
        float allocatedRam = GC.GetTotalMemory(false) / (1024f * 1024f);
        float totalReservedRam = UnityEngine.Profiling.Profiler.GetTotalReservedMemoryLong() / (1024f * 1024f);

        // Modo de Operacao atual
        string opMode = OperationModeManager.Current.ToString();

        // Contagem de kits (se houver referencia ou procura de tags)
        int activeKits = 0;
        GameObject[] kits = GameObject.FindGameObjectsWithTag("Respawn"); // Fallback ou contagem de kits
        var basyx = FindFirstObjectByType<BaSyxManager>();
        if (basyx != null)
        {
            // Usamos a contagem atraves de GameObjects nomeados ou transform
            activeKits = GameObject.FindObjectsByType<DynamicProduct>(FindObjectsSortMode.None).Length;
        }

        MetricEntry entry = new MetricEntry
        {
            timeSinceStart = Time.realtimeSinceStartup,
            fps = currentFps,
            frameTimeMs = frameTimeMs,
            ramAllocatedMB = allocatedRam,
            ramTotalReservedMB = totalReservedRam,
            operationMode = opMode,
            activeKitsCount = activeKits,
            startupTimeSec = startupRecordedTime >= 0 ? startupRecordedTime : -1f,
            isStartupDone = BaSyxManager.IsStartupComplete
        };

        recordedEntries.Add(entry);
    }

    public void SaveToCSV()
    {
        if (recordedEntries.Count == 0) return;

        StringBuilder sb = new StringBuilder();

        // Cabecalho com colunas descritivas
        sb.AppendLine("Timestamp_s,FPS,FrameTime_ms,RAM_Allocated_MB,RAM_TotalReserved_MB,OperationMode,ActiveProducts,StartupTime_s,IsStartupComplete");

        for (int i = 0; i < recordedEntries.Count; i++)
        {
            var e = recordedEntries[i];
            sb.AppendLine(string.Format(CultureInfo.InvariantCulture,
                "{0:F3},{1:F2},{2:F2},{3:F2},{4:F2},{5},{6},{7:F3},{8}",
                e.timeSinceStart,
                e.fps,
                e.frameTimeMs,
                e.ramAllocatedMB,
                e.ramTotalReservedMB,
                e.operationMode,
                e.activeKitsCount,
                e.startupTimeSec,
                e.isStartupDone ? "1" : "0"
            ));
        }

        File.WriteAllText(csvFilePath, sb.ToString());
        Debug.Log($"[PerformanceDataLogger] 💾 Dados guardados com sucesso ({recordedEntries.Count} registos) em:\n{csvFilePath}");
    }

    void OnApplicationQuit()
    {
        if (autoSaveOnQuit)
        {
            SaveToCSV();
        }
    }
}
