"""Optional noninteractive Windows Service via pywin32.

Install with the service account/environment configured per docs.
This service supervises the Mini App and (only if configured) an OWNED Hermes
gateway subprocess. Unrelated existing Hermes installations are not killed.
"""

from __future__ import annotations

import logging
import os
import sys

from .supervisor import GatewaySupervisor, worker_commands

LOG = logging.getLogger("hermes.windows_service")

if sys.platform == "win32":
    import servicemanager
    import win32event
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
                if (os.environ.get("HERMES_GATEWAY_COMMAND") and
                        os.environ.get("USERNAME", "").lower() in ("system", "localsystem")):
                    raise RuntimeError(
                        "Refusing to launch Hermes Agent tools as LocalSystem. "
                        "Configure a restricted Windows service account."
                    )
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
