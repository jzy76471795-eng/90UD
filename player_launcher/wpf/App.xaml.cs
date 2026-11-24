using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;

namespace DNFLogin
{
    public partial class App : System.Windows.Application
    {
        private Mutex? _mutex;
        private EventWaitHandle? _showSignal;
        private bool _packageStartupRejected;
#if PYTHON90
        private Services.PythonRechargeBridge? _rechargeBridge;
#endif

        protected override void OnStartup(StartupEventArgs e)
        {
#if PYTHON90
            // Validate the public package before reading any saved settings or acquiring an instance name.
            try { Services.LauncherPackage.Initialize(Services.PythonLauncherPaths.LauncherDirectory); }
            catch (InvalidOperationException error)
            {
                _packageStartupRejected = true;
                MessageBox.Show(error.Message, "登录器配置错误", MessageBoxButton.OK, MessageBoxImage.Warning);
                Shutdown();
                return;
            }
#endif
            base.OnStartup(e);

            // 全局异常兜底：未处理异常记录日志并阻止直接闪退（async void / 后台线程异常不再静默崩溃）
            DispatcherUnhandledException += OnDispatcherUnhandledException;
            TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;

            // 单实例互斥：已存在实例时通知其弹出窗口，然后退出本次启动
            var mutexName = @"Local\DNFLoginPy90_SingleInstance";
            var signalName = @"Local\DNFLoginPy90_ShowSignal";
#if PYTHON90
            mutexName = Services.LauncherPackage.MutexName;
            signalName = Services.LauncherPackage.ShowSignalName;
#endif
            _mutex = new Mutex(true, mutexName, out bool createdNew);
            if (!createdNew)
            {
                try
                {
                    using var signal = EventWaitHandle.OpenExisting(signalName);
                    signal.Set();
                }
                catch
                {
#if PYTHON90
                    MessageBox.Show("同一服务标识的另一个登录器正在运行。请先关闭原窗口，再打开这个地址的新包。", "登录器已打开", MessageBoxButton.OK, MessageBoxImage.Information);
#endif
                }

                Shutdown();
                return;
            }

            // 启动时读取持久化配置（AppData\DNFLogin\config.json）
            AppConfig.Load();
#if PYTHON90
            Services.PythonClientLauncher.LockSettings();
            try
            {
                _rechargeBridge=new Services.PythonRechargeBridge(async cancellation =>
                {
                    using var api=Services.PythonClientLauncher.CreateApi();
                    return await api.RechargeUrlAsync().WaitAsync(cancellation).ConfigureAwait(false);
                },message=>LogStartup(message));
            }
            catch(System.Net.Sockets.SocketException)
            {
                LogStartup("recharge listener unavailable");
            }
#endif

            // 启动自检：JSON 缺少新字段（如 CloseActionAsked）时补齐默认值
            AppConfig.SelfCheck();

#if EXTERNAL
            // M4 外网版启动自检：client.yh 验签 + target 版本一致性（同目录优先并转移覆盖到
            // %ProgramData%\DNFLogin\secure\，失败弹窗说明并拒绝启动）。仅 EXTERNAL 编译进外网版，
            // Free 版不经过该校验、零影响。置于窗口创建之前，实现早期拦截。
            if (!Services.ClientYh.ClientYhGuard.RunStartupCheck())
            {
                Shutdown();
                return;
            }
#endif

            // 若上次开启了按键连发，登录器启动即常驻连发引擎（登录器即连发本身）
            // ASSIST-OFF-EXTERNAL（2026-09-20）：按键连发属于"游戏助手"功能，外网版整块停用
            // （外网版不能再依赖"页面被折叠所以用户开不了"——配置是共享的，开关残留会照样启动引擎）。
            // 恢复：删掉这对 #if。
#if !EXTERNAL && !PYTHON90
            if (AppConfig.Settings.AutoFireEnabled)
            {
                Services.AutoFireEngine.Start(AppConfig.Settings.AutoFireKeys);
            }
#endif

            // 监听"已有实例请求弹出窗口"信号，收到后把主窗口带到前台
            _showSignal = new EventWaitHandle(false, EventResetMode.AutoReset, signalName);
            var watcher = new Thread(() =>
            {
                try
                {
                    while (_showSignal.WaitOne())
                    {
                        Dispatcher.Invoke(BringCurrentWindowToFront);
                    }
                }
                catch
                {
                    // 退出时句柄被关闭，监听线程随之结束
                }
            })
            { IsBackground = true };
            watcher.Start();
        }

        private static string LogDirectory =>
#if PYTHON90
            Services.LauncherPackage.ConfigurationDirectory;
#else
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "DNFLoginPy90");
#endif
        private static string CrashLogPath => Path.Combine(LogDirectory, "crash.log");

        public static void LogStartup(string message)
        {
            if (Services.ClientDiagnostics.Detailed(LogDirectory))
                Services.ClientDiagnostics.Append(LogDirectory, "startup.log", message);
        }

        private void OnDispatcherUnhandledException(object sender, System.Windows.Threading.DispatcherUnhandledExceptionEventArgs e)
        {
            Services.ClientDiagnostics.Append(LogDirectory, "crash.log",
                $"{e.Exception.GetType().Name} HResult={e.Exception.HResult}");

            MessageBox.Show("程序发生未处理异常，操作已中断。\n\n" + e.Exception.GetType().Name +
                            "\n\n详细错误已记录到：" + CrashLogPath,
                "异常提示", MessageBoxButton.OK, MessageBoxImage.Warning);
            e.Handled = true;
        }

        private void OnUnobservedTaskException(object? sender, UnobservedTaskExceptionEventArgs e)
        {
            Services.ClientDiagnostics.Append(LogDirectory, "crash.log",
                $"[UnobservedTask] {e.Exception.GetType().Name} HResult={e.Exception.HResult}");

            e.SetObserved();
        }

        private static void BringCurrentWindowToFront()
        {
            var main = Current.MainWindow;
            switch (main)
            {
                case LoginWindow login:
                    login.BringToFront();
                    break;
                case null:
                    return;
                default:
                    if (main.WindowState == WindowState.Minimized)
                    {
                        main.WindowState = WindowState.Normal;
                    }

                    main.Show();
                    main.Activate();
                    main.Topmost = true;
                    main.Topmost = false;
                    break;
            }
        }

        /// <summary>
        /// 清除所有配置前的重启准备：释放单实例互斥与弹窗信号，
        /// 使随后启动的新实例能获得单实例所有权正常启动（否则新进程会因已有实例而立即退出）。
        /// 调用后本进程应尽快 Shutdown。
        /// </summary>
        public void PrepareForRestart()
        {
            try
            {
                _showSignal?.Close();
            }
            catch
            {
                // 忽略释放异常
            }
            _showSignal = null;

            try
            {
                _mutex?.ReleaseMutex();
            }
            catch
            {
                // 非持有线程释放会抛异常，Dispose 兜底释放句柄
            }

            try
            {
                _mutex?.Dispose();
            }
            catch
            {
                // 忽略释放异常
            }
            _mutex = null;
        }

        protected override void OnExit(ExitEventArgs e)
        {
#if PYTHON90
            _rechargeBridge?.Dispose();
            _rechargeBridge=null;
#endif
            if (!_packageStartupRejected) App.LogStartup("App.OnExit: 应用正在退出");
            // 退出时停止连发引擎并复位所有按键，避免游戏内卡键


            _showSignal?.Close();
            _showSignal = null;

            if (_mutex is not null)
            {
                try
                {
                    _mutex.ReleaseMutex();
                }
                catch
                {
                    // 非拥有线程释放会抛异常，忽略
                }

                _mutex.Dispose();
                _mutex = null;
            }

            base.OnExit(e);
        }
    }
}
