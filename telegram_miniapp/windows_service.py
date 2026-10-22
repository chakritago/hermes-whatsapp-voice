"""Optional noninteractive Windows Service via pywin32.

Install with the service account/environment configured per docs.
This service supervises the Mini App and (only if configured) an OWNED Hermes
gateway subprocess. Unrelated existing Hermes installations are not killed.
"""

from __future__ import annotations

import logging
import sys

from .supervisor import GatewaySupervisor, worker_commands

LOG = logging.getLogger("hermes.windows_service")

if sys.platform == "win32":
    import servicemanager
    import win32api
    import win32con
    import win32event
    import win32security
    import win32service
    import win32serviceutil

    class HermesTelegramService(win32serviceutil.ServiceFramework):
        _svc_name_ = "HermesTelegramVoice"
        _svc_display_name_ = "Hermes Telegram Voice"
        _svc_description_ = "Supervise Hermes Telegram Mini App and optionally its owned gateway"

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.running = True
            self.supervisor = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.running = False
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogInfoMsg("Hermes Telegram Voice service starting")
            try:
                # LocalSystem (SID S-1-5-18) is forbidden even without an owned
                # gateway: the Mini App holds a key granting Hermes tool access.
                token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
                sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
                if win32security.ConvertSidToStringSid(sid) == "S-1-5-18":
                    raise RuntimeError("Configure a dedicated restricted service account; LocalSystem is forbidden")
                self.supervisor = GatewaySupervisor(*worker_commands())
                while self.running:
                    self.supervisor.tick()
                    win32event.WaitForSingleObject(self.stop_event, 5000)
            except Exception:
                LOG.exception("Hermes service failure")
                servicemanager.LogErrorMsg("Hermes Telegram Voice service failed")
                raise
            finally:
                if self.supervisor:
                    self.supervisor.stop()

    if __name__ == "__main__":
        win32serviceutil.HandleCommandLine(HermesTelegramService)
else:
    if __name__ == "__main__":
        raise SystemExit("This service is available on Windows only")
