#!/usr/bin/env python3

import subprocess
import time
import os
import signal
import sys
import logging
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# --- Configuration ---
ROUTER_IP = "192.168.8.1"
ROUTER_PORT = "5555"
TTY_DEVICE = "/dev/ttyGPS0"
BAUD_RATE = "115200"
GPS_DBUS_PATH = "/opt/victronenergy/gps-dbus/gps_dbus"
SOCAT_PATH = "/usr/bin/socat"

# Monitoring thresholds
MAX_IDLE_TIME_SECONDS = 120  # Max time allowed without a new GPS fix (2 minutes)
WATCHDOG_CHECK_INTERVAL = 30 # How often to check the DBus timestamp/process status

# DBus configuration for monitoring
DBUS_SERVICE = 'com.victronenergy.gps'
DBUS_PATH_LAST_UPDATE = '/TimeSinceLastUpdate'

# --- Global Process and Logger Variables ---
logger = None

def setup_logging():
    """Sets up standard output logging for multilog capture."""
    global logger
    logger = logging.getLogger('GpsService')
    logger.setLevel(logging.INFO)
    
    # Remove any existing handlers to prevent duplicate logs on service restart
    if logger.hasHandlers():
        logger.handlers.clear()
    
    # Stream Handler: Directs all logs to sys.stdout for multilog to capture
    stream_handler = logging.StreamHandler(sys.stdout)
    # Define the desired log format
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    
    logger.info("Service logging configured for multilog capture.")
    return logger

class GpsServiceManager:
    def __init__(self):
        self.socat_process = None
        self.gps_dbus_process = None

        logger.info("--- Initializing GPS Service Manager ---")
        
        if not self._install_socat():
            # If socat fails to install, exit with error so runit retries
            sys.exit(1)

        self._start_services()

        # Start the GLib Watchdog (the main monitoring loop)
        GLib.timeout_add_seconds(WATCHDOG_CHECK_INTERVAL, self._watchdog_monitor)
        
    def _install_socat(self):
        """Checks for and installs socat using opkg if necessary."""
        logger.info("Checking for socat installation...")
        if not os.path.exists(SOCAT_PATH):
            logger.warning("socat not found. Attempting installation via opkg...")
            try:
                subprocess.run(["opkg", "update"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["opkg", "install", "socat"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info("socat installed successfully.")
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to install socat: {e}")
                return False
            except FileNotFoundError:
                 logger.error("opkg command not found. Cannot install socat.")
                 return False
        else:
            logger.info("socat is already installed.")
        return True

    def _start_services(self):
        """Starts the socat and gps_dbus processes."""
        self._stop_services() # Clean state

        # 1. Start socat
        socat_cmd = [
            SOCAT_PATH,
            f"TCP:{ROUTER_IP}:{ROUTER_PORT}",
            f"pty,link={TTY_DEVICE},raw,nonblock,echo=0,b{BAUD_RATE}"
        ]
        logger.info(f"Starting socat: {' '.join(socat_cmd)}")
        try:
            self.socat_process = subprocess.Popen(socat_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        except Exception as e:
            logger.error(f"Failed to launch socat: {e}")
            return False

        time.sleep(2) # Give time for TTY link to be created

        # 2. Start gps_dbus
        gps_dbus_cmd = [
            GPS_DBUS_PATH,
            "-s", TTY_DEVICE,
            "-b", BAUD_RATE,
            "-t", "0"
        ]
        logger.info(f"Starting gps_dbus: {' '.join(gps_dbus_cmd)}")
        try:
            self.gps_dbus_process = subprocess.Popen(gps_dbus_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        except Exception as e:
            logger.error(f"Failed to launch gps_dbus: {e}")
            self._stop_services()
            return False
            
        logger.info(f"Services started. socat PID: {self.socat_process.pid}, gps_dbus PID: {self.gps_dbus_process.pid}")
        return True

    def _stop_services(self):
        """Stops the socat and gps_dbus processes."""
        
        for proc in [self.gps_dbus_process, self.socat_process]:
            if proc and proc.poll() is None:
                logger.info(f"Stopping process (PID: {proc.pid})")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                     proc.kill()
                     logger.warning(f"Process (PID: {proc.pid}) did not stop gracefully, killed.")
                     
        self.socat_process = None
        self.gps_dbus_process = None
        logger.info("All services stopped.")

    def _get_dbus_timestamp(self):
        """Reads the /TimeSinceLastUpdate path via dbus-send."""
        try:
            dbus_cmd = [
                'dbus-send', 
                '--system', 
                '--print-reply', 
                '--type=method_call', 
                '--dest=com.victronenergy.gps', 
                DBUS_PATH_LAST_UPDATE, 
                'org.freedesktop.DBus.Properties.Get', 
                'string:com.victronenergy.BusItem', 
                'string:Value'
            ]
            
            proc = subprocess.Popen(dbus_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate(timeout=5)
            
            if 'double' in stdout:
                parts = stdout.split()
                value_str = parts[parts.index('double') + 1]
                return float(value_str)
                
        except Exception as e:
            logger.debug(f"Could not read DBus timestamp: {e}")
            return None
            
        return None

    def _watchdog_monitor(self):
        """GLib Timeout handler: Checks process status and data freshness."""
        
        # 1. Check if processes are running
        if self.socat_process is None or self.socat_process.poll() is not None:
            logger.error("socat process died unexpectedly! Initiating restart.")
            self._stop_services()
            self._start_services()
            return True
        
        if self.gps_dbus_process is None or self.gps_dbus_process.poll() is not None:
            logger.error("gps_dbus process died unexpectedly! Initiating restart.")
            self._stop_services()
            self._start_services()
            return True

        # 2. Check DBus for data activity (Idle check)
        time_since_last_update = self._get_dbus_timestamp()
        
        if time_since_last_update is not None:
            logger.info(f"Time since last GPS fix: {time_since_last_update:.2f} seconds.")
            
            if time_since_last_update > MAX_IDLE_TIME_SECONDS:
                logger.error(f"GPS data is stale! Idle for {time_since_last_update:.2f}s. Initiating restart.")
                self._stop_services()
                self._start_services()
        else:
            logger.warning("Could not read /TimeSinceLastUpdate from DBus. May be a transient issue.")
            
        return True # Continue monitoring

def signal_handler(sig, frame):
    """Gracefully handles termination signals (TERM, INT)."""
    logger.info(f"Received signal {sig}. Stopping MainLoop.")
    GLib.MainLoop().quit()
    
# --- Main Execution ---
if __name__ == "__main__":
    
    setup_logging()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    DBusGMainLoop(set_as_default=True)
    manager = GpsServiceManager() 
    
    mainloop = GLib.MainLoop()
    try:
        mainloop.run()
    except Exception as e:
        logger.error(f"Main loop encountered an error: {e}. Exiting.")
        
    # Ensure external processes are stopped upon MainLoop exit
    manager._stop_services()
    logger.info("Service process finished.")
