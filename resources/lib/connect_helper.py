import threading
from utils import log_msg, log_exception
from spotty import Spotty

class ConnectHelper(threading.Thread):
    """keeps a persistant spotty instance open in a seperate thread to handle SpotifyConnect"""
    spotty_proc = None
    daemon_active = False

    def __init__(self, spotty: Spotty):
        self.__spotty = spotty
        threading.Thread.__init__(self)
        self.daemon = True

    def stop(self):
        log_msg("spotty connect daemon exiting")
        if self.spotty_proc:
            self.spotty_proc.terminate()
            log_msg("terminated spotty connect daemon")
        self.join(2)

    def run(self):
        log_msg("spotty connect daemon starting!")
        try:
            args = [
                "--zeroconf-port",
                "1234",
            ]
            self.spotty_proc = self.__spotty.run_spotty(args, use_creds=True)
            self.daemon_active = True
        except Exception as ex:
            self.__log_exception_sending(ex, range_begin, bytes_sent)

    def __log_exception_sending(self, ex: Exception, range_begin: int, bytes_sent: int) -> None:
        log_msg(
            f"EXCEPTION sending track '{self.__track_id}'"
            f" - range begin {range_begin}"
            f" - range end {bytes_sent} - {self.__get_mb_str(bytes_sent)}.",
            LOGERROR,
        )
        log_msg(f"Exception: {ex}")
