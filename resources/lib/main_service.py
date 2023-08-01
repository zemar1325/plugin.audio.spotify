#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
    plugin.audio.spotify
    Spotify Player for Kodi
    main_service.py
    Background service which launches the spotty binary and monitors the player
"""

from utils import log_msg, ADDON_ID, get_token, Spotty
from httpproxy import ProxyRunner
import xbmc
import xbmcaddon
import xbmcgui
import spotipy
import time


class MainService:
    """our main background service running the various threads"""

    def __init__(self):
        self.current_user = None
        self.auth_token = None
        self.addon = xbmcaddon.Addon(id=ADDON_ID)
        self.win = xbmcgui.Window(10000)
        self.kodimonitor = xbmc.Monitor()
        self.spotty = Spotty()

        # spotipy and the webservice are always prestarted in the background.
        # The auth key for spotipy will be set afterwards.
        # The webserver is also used for the authentication callbacks from spotify api.
        self.sp = spotipy.Spotify()

        self.proxy_runner = ProxyRunner(self.spotty)
        self.proxy_runner.start()
        webport = self.proxy_runner.get_port()
        log_msg('started webproxy at port {0}'.format(webport))

        # Authenticate at startup.
        self.renew_token()

        # Start mainloop.
        self.main_loop()

    def main_loop(self):
        """main loop which keeps our threads alive and refreshes the token"""
        loop_timer = 5
        while not self.kodimonitor.waitForAbort(loop_timer):
            # Monitor logged in user.
            cmd = self.win.getProperty("spotify-cmd")
            if cmd == "__LOGOUT__":
                log_msg("logout cmd received")
                self.win.clearProperty("spotify-cmd")
                self.current_user = None
                self.auth_token = None
                self.switch_user()
            elif not self.auth_token:
                # We do not yet have a token.
                log_msg("retrieving token...")
                if self.renew_token():
                    xbmc.executebuiltin("Container.Refresh")
            elif self.auth_token and (self.auth_token['expires_at'] - 60) <= (int(time.time())):
                log_msg("Token needs to be refreshed.")
                self.renew_token()
            else:
                loop_timer = 5

        # End of loop: we should exit.
        self.close()

    def close(self):
        """shutdown, perform cleanup"""
        log_msg('Shutdown requested!', xbmc.LOGINFO)
        self.spotty.kill_spotty()
        self.proxy_runner.stop()
        del self.addon
        del self.kodimonitor
        del self.win
        log_msg('Stopped', xbmc.LOGINFO)

    def switch_user(self):
        """called whenever we switch to a different user/credentials"""
        log_msg("Login credentials changed.")
        if self.renew_token():
            xbmc.executebuiltin("Container.Refresh")

    def get_username(self):
        """ get the current configured/setup username"""
        username = self.spotty.get_username()
        if not username:
            username = self.addon.getSetting("username")
            if not username and self.addon.getSetting("multi_account") == "true":
                username1 = self.addon.getSetting("username1")
                password1 = self.addon.getSetting("password1")
                if username1 and password1:
                    self.addon.setSetting("username", username1)
                    self.addon.setSetting("password", password1)
                    username = username1
        return username

    def renew_token(self):
        """refresh/retrieve the token"""
        result = False
        auth_token = None
        username = self.get_username()

        if username:
            # Stop connect daemon.
            # Retrieve token.
            log_msg("Retrieving auth token....")
            auth_token = get_token(self.spotty)

        if auth_token:
            log_msg("Retrieved auth token.")
            self.auth_token = auth_token
            # Only update token info in spotipy object.
            self.sp._auth = auth_token["access_token"]
            me = self.sp.me()
            self.current_user = me["id"]
            log_msg(f"Logged in to Spotify - Username: {self.current_user}", xbmc.LOGINFO)
            # Store auth_token and username as window prop for easy access by plugin entry.
            self.win.setProperty("spotify-token", auth_token["access_token"])
            self.win.setProperty("spotify-username", self.current_user)
            self.win.setProperty("spotify-country", me["country"])
            result = True

        return result
