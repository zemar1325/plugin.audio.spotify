# -*- coding: utf-8 -*-
import json
import math
import threading
import time

import xbmc
import xbmcaddon

import cherrypy
from cherrypy._cpnative_server import CPHTTPServer
from utils import create_wave_header, log_msg, log_exception, PROXY_PORT, ADDON_ID

LIBRESPOT_INITIAL_VOLUME = "50"
SPOTTY_AUDIO_CHUNK_SIZE = 524288
SPOTIFY_TRACK_PREFIX = "spotify:track:"


class Root:
    def __init__(self, spotty):
        self.spotty = spotty
        self.spotty_bin = None
        self.spotty_trackid = None
        self.spotty_range_l = None
        self.requested_kodi_volume = self.get_spotify_volume_setting()
        self.kodi_volume_has_been_reset = False
        self.saved_volume = -1

    @staticmethod
    def get_spotify_volume_setting():
        requested_kodi_volume = xbmcaddon.Addon(id=ADDON_ID).getSetting("initial_volume")
        if not requested_kodi_volume:
            return -1
        requested_kodi_volume = int(requested_kodi_volume)
        if (requested_kodi_volume < -1) or (requested_kodi_volume > 100):
            raise Exception(
                f'Invalid initial volume "{requested_kodi_volume}".'
                f" Must in the range [-1, 100]."
            )
        return int(requested_kodi_volume)

    @staticmethod
    def get_current_playback_volume():
        volume_query = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "Application.GetProperties",
            "params": {"properties": ["volume", "muted"]},
        }
        result = xbmc.executeJSONRPC(json.dumps(volume_query))
        result = json.loads(result)
        result = result.get("result")
        return result["volume"]

    @staticmethod
    def set_volume(percent_value):
        xbmc.executeJSONRPC(
            f'{{"jsonrpc":"2.0","method":"Application.SetVolume",'
            f'"id":1,"params":{{"volume": {percent_value}}}}}'
        )

    def reset_kodi_volume(self):
        if self.kodi_volume_has_been_reset or self.requested_kodi_volume == -1:
            return

        self.set_volume(self.requested_kodi_volume)
        time.sleep(0.5)
        if self.requested_kodi_volume != self.get_current_playback_volume():
            raise Exception(
                f'Error: Could not set spotify volume to "{self.requested_kodi_volume}".'
            )
        self.kodi_volume_has_been_reset = True
        log_msg(
            f"Saved volume: {self.saved_volume}%,"
            f" new spotify volume: {self.requested_kodi_volume}%.",
            xbmc.LOGDEBUG,
        )

    def reset_volume_to_saved(self):
        if not self.kodi_volume_has_been_reset:
            return

        time.sleep(0.2)
        self.set_volume(self.saved_volume)
        self.kodi_volume_has_been_reset = False
        log_msg(f"Reset volume to saved volume: {self.saved_volume}%.", xbmc.LOGDEBUG)

    @staticmethod
    def _check_request():
        method = cherrypy.request.method.upper()
        # headers = cherrypy.request.headers
        # Fail for other methods than get or head
        if method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
        # Error if the requester is not allowed
        # for now this is a simple check just checking if the useragent matches Kodi
        # user_agent = headers['User-Agent'].lower()
        # if not ("Kodi" in user_agent or "osmc" in user_agent):
        #     raise cherrypy.HTTPError(403)
        return method

    @cherrypy.expose
    def index(self):
        return "Server started"

    @cherrypy.expose
    def track(self, track_id, duration):
        # Check sanity of the request
        self._check_request()

        # Calculate file size, and obtain the header
        duration = int(float(duration))
        wave_header, filesize = create_wave_header(duration)
        request_range = cherrypy.request.headers.get("Range", "")
        # Response timeout must be at least the duration of the track read/write loop.
        # Checks for timeout and stops pushing audio to player if it occurs.
        cherrypy.response.timeout = int(math.ceil(duration * 1.5))

        range_l = 0
        range_r = filesize

        # headers
        if request_range and request_range != "bytes=0-":
            # Partial request.
            cherrypy.response.status = "206 Partial Content"
            cherrypy.response.headers["Content-Type"] = "audio/x-wav"
            rng = cherrypy.request.headers["Range"].split("bytes=")[1].split("-")
            log_msg(f"Request header range: {cherrypy.request.headers['Range']}", xbmc.LOGDEBUG)
            range_l = int(rng[0])
            try:
                range_r = int(rng[1])
            except:
                range_r = filesize

            cherrypy.response.headers["Accept-Ranges"] = "bytes"
            cherrypy.response.headers["Content-Length"] = range_r - range_l
            cherrypy.response.headers["Content-Range"] = f"bytes {range_l}-{range_r}/{filesize}"
            log_msg(
                f"Partial request range: {cherrypy.response.headers['Content-Range']},"
                f" length: {cherrypy.response.headers['Content-Length']}",
                xbmc.LOGDEBUG,
            )
        else:
            # Full file
            cherrypy.response.headers["Content-Type"] = "audio/x-wav"
            cherrypy.response.headers["Accept-Ranges"] = "bytes"
            cherrypy.response.headers["Content-Length"] = filesize
            log_msg(f"Full File. Size: {filesize}.", xbmc.LOGDEBUG)
            log_msg(f"Track ended?", xbmc.LOGDEBUG)
            self.reset_volume_to_saved()

        # If method was GET, then write the file content.
        if cherrypy.request.method.upper() == "GET":
            if self.spotty_bin is not None:
                # If spotty binary still attached for a different request, try to terminate it.
                log_msg(
                    "WHOOPS!!! A running 'spotty' detected - killing it to continue.", xbmc.LOGERROR
                )
                self.kill_spotty()

            while self.spotty_bin:
                time.sleep(0.1)

            return self.send_audio_stream(track_id, range_r - range_l, wave_header, range_l)

    track._cp_config = {"response.stream": True}

    def kill_spotty(self):
        self.spotty_bin.terminate()
        self.spotty_bin.communicate()
        self.spotty_bin = None
        self.spotty_trackid = None
        self.spotty_range_l = None
        log_msg("Killing spotty.")

    def send_audio_stream(self, track_id, length, wave_header, range_l):
        """Chunked transfer of audio data from spotty binary"""
        bytes_written = 0

        try:
            self.reset_kodi_volume()

            log_msg(f"Start transfer for track {track_id} - range: {range_l}", xbmc.LOGDEBUG)

            # Write wave header.
            # Only count bytes actually from the spotify stream.
            if not range_l:
                yield wave_header
                bytes_written = len(wave_header)

            # Get data from spotty stdout and append to our buffer.
            track_id_uri = SPOTIFY_TRACK_PREFIX + track_id
            args = [
                "-n",
                "temp",
                "--enable-volume-normalisation",
                "--normalisation-gain-type",
                "track",
                "--initial-volume",
                LIBRESPOT_INITIAL_VOLUME,
                "--single-track",
                track_id_uri,
            ]
            if self.spotty_bin is None:
                self.spotty_bin = self.spotty.run_spotty(args, use_creds=True)
            if not self.spotty_bin.returncode:
                log_msg(f"returncode: {self.spotty_bin.returncode}", xbmc.LOGDEBUG)

            self.spotty_trackid = track_id
            self.spotty_range_l = range_l

            log_msg(f"Reading track uri: {track_id_uri}, length = {length}", xbmc.LOGDEBUG)

            # Ignore the first x bytes to match the range request.
            if range_l:
                self.spotty_bin.stdout.read(range_l)

            # Loop as long as there's something to output.
            while bytes_written < length:
                frame = self.spotty_bin.stdout.read(SPOTTY_AUDIO_CHUNK_SIZE)
                if not frame:
                    log_msg("Nothing read from stdout.", xbmc.LOGDEBUG)
                    break
                bytes_written += len(frame)
                log_msg(
                    f"Continuing transfer for track {track_id} - bytes written = {bytes_written}",
                    xbmc.LOGDEBUG,
                )
                yield frame

            log_msg(
                f"FINISHED transfer for track {track_id}"
                f" - range {range_l} - bytes written {bytes_written}.",
                xbmc.LOGDEBUG,
            )
        except Exception:
            log_msg(
                "EXCEPTION FINISH transfer for track {track_id}"
                f" - range {range_l} - bytes written {bytes_written}.",
                xbmc.LOGERROR,
            )
            log_exception("Error with track transfer")
        finally:
            # Make sure spotty always gets terminated.
            if self.spotty_bin is not None:
                self.kill_spotty()

    @cherrypy.expose
    def callback(self, **kwargs):
        cherrypy.response.headers["Content-Type"] = "text/html"
        code = kwargs.get("code")
        url = "http://localhost:%s/callback?code=%s" % (PROXY_PORT, code)
        if cherrypy.request.method.upper() in ["GET", "POST"]:
            html = "<html><body><h1>Authentication succesful</h1>"
            html += "<p>You can now close this browser window.</p>"
            html += "</body></html>"
            xbmc.executebuiltin("SetProperty(spotify-token-info,%s,Home)" % url)
            log_msg("authkey sent")
            return html

    @cherrypy.expose
    def playercmd(self, cmd):
        if cmd == "start":
            cherrypy.response.headers["Content-Type"] = "text"
            log_msg("Playback start requested by connect.")
            xbmc.executebuiltin("RunPlugin(plugin://plugin.audio.spotify/?action=play_connect)")
            return "OK"
        elif cmd == "stop":
            cherrypy.response.headers["Content-Type"] = "text"
            log_msg("Playback stop requested by connect")
            xbmc.executebuiltin("PlayerControl(Stop)")
            return "OK"


class ProxyRunner(threading.Thread):
    def __init__(self, spotty):
        self.__root = Root(spotty)
        log = cherrypy.log
        log.screen = True
        cherrypy.config.update(
            {"server.socket_host": "127.0.0.1", "server.socket_port": PROXY_PORT}
        )
        self.__server = cherrypy.server.httpserver = CPHTTPServer(cherrypy.server)
        threading.Thread.__init__(self)

    def run(self):
        conf = {"/": {}}
        cherrypy.quickstart(self.__root, "/", conf)

    def get_port(self):
        return self.__server.bind_addr[1]

    def get_host(self):
        return self.__server.bind_addr[0]

    def stop(self):
        cherrypy.engine.exit()
        self.join(0)
        del self.__root
        del self.__server
