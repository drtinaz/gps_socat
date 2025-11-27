#!/usr/bin/env python3

import subprocess
import time
import os
import signal
import sys
import logging
import configparser
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# --- Configuration File Path ---
CONFIG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

# --- Global Logger Variable ---
logger = None

def load_config():
    """Loads all configuration settings from the config.ini file."""
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE_PATH):
        print(f"ERROR: Configuration file not found at {CONFIG_FILE_PATH}", file=sys.stderr)
        sys.exit(1)
        
    config.read(CONFIG_FILE_PATH)
    
    try:
        cfg = {
            'router_ip': config['CONNECTION']['router_ip'],
            'router_port': config.getint('CONNECTION', 'router_port'),
            'tty_device': config['CONNECTION']['tty_device'],
            'baud_rate': config['CONNECTION']['baud_rate'],
            
            'gps_dbus_path': config['PATHS']['gps_dbus_path'],
            'socat_path': config['PATHS']['socat_path'],
            
            'max_idle_time_seconds': config.getint('MONITORING', 'max_idle_time_seconds'),
            'watchdog_check_interval': config.getint('MONITORING', 'watchdog_check_interval'),
            'dbus_service': config['MONITORING']['dbus_service'],
            'dbus_path_last_update': config['MONITORING']['dbus_path_last_update'], # Now /NumberOfSatellites
        }
        return cfg
    except KeyError as e:
        print(f"ERROR: Missing configuration key in config.ini: {e}", file=sys.stderr)
        sys.exit(1)

def setup_logging():
    """Sets up standard output logging for multilog capture."""
    global logger
    logger = logging.getLogger('GpsService')
    logger.setLevel(logging.INFO)
    
    if logger.hasHandlers():
        logger.handlers.clear()
    
    stream_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    
    logger.info("Service logging configured for multilog capture.")
    return logger

class GpsServiceManager:
    def __init__(self):
        self.config = load_config()
        self.socat_process = None
        self.gps_dbus_process = None
        self.last_good_data_time = time.time() # NEW: Track the last time satellites were seen

        logger.info("--- Initializing GPS Service Manager ---")
        
        if not self._install_socat():
            sys.exit(1)

        self._start_services()

        GLib.timeout_add_seconds(self.config['watchdog_check_interval'], self._watchdog_monitor)
        
    def _install_socat(self):
        """Checks for and installs socat using opkg if necessary."""
        logger.info("Checking for socat installation...")
        if not os.path.exists(self.config['socat_path']):
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
        self._stop_services() 

        # 1. Start socat
        socat_cmd = [
            self.config['socat_path'],
            f"TCP:{self.config['router_ip']}:{self.config['router_port']}",
            f"pty,link={self.config['tty_device']},raw,nonblock,echo=0,b{self.config['baud_rate']}"
        ]
        logger.info(f"Starting socat: {' '.join(socat_cmd)}")
        try:
            self.socat_process = subprocess.Popen(socat_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        except Exception as e:
            logger.error(f"Failed to launch socat: {e}")
            return False

        time.sleep(2) 

        # 2. Start gps_dbus
        gps_dbus_cmd = [
            self.config['gps_dbus_path'],
            "-s", self.config['tty_device'],
            "-b", self.config['baud_rate'],
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

# ... other GpsServiceManager methods ...

    def _get_dbus_satellite_count(self):
        """
        Reads the /NumberOfSatellites path.
        Returns the integer count, or None on failure/missing data.
        """
        try:
            dbus_cmd = [
                'dbus-send', 
                '--system', 
                '--print-reply', 
                '--type=method_call', 
                f"--dest={self.config['dbus_service']}", 
                self.config['dbus_path_last_update'], # This is now /NumberOfSatellites
                'org.freedesktop.DBus.Properties.Get', 
                'string:com.victronenergy.BusItem', 
                'string:Value'
            ]
            
            proc = subprocess.Popen(dbus_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate(timeout=5)
            
            # The output for /NumberOfSatellites is typically 'variant       int32 9'
            if 'int32' in stdout:
                parts = stdout.split()
                # Find the index of 'int32' and the value is the next part
                value_str = parts[parts.index('int32') + 1]
                return int(value_str)
                
        except Exception as e:
            logger.debug(f"Could not read DBus path {self.config['dbus_path_last_update']}: {e}")
            return None
            
        return None

    def _watchdog_monitor(self):
        """GLib Timeout handler: Checks process status and data presence."""
        
        # 1. Check if processes are running (No change here)
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

        # 2. Check DBus for data activity (Fix/Satellite check)
        satellite_count = self._get_dbus_satellite_count()
        max_idle = self.config['max_idle_time_seconds']

        if satellite_count is not None and satellite_count > 0:
            # We have good data (non-zero satellites); reset the timer
            self.last_good_data_time = time.time()
            logger.info(f"GPS Fix Active. Satellites: {satellite_count}. Monitoring continues.")
        else:
            # Data is invalid (0 satellites or dbus read failed)
            time_since_last_fix = time.time() - self.last_good_data_time
            
            logger.warning(f"No valid satellite count ({satellite_count}). Time since last good fix: {time_since_last_fix:.0f}s.")
            
            if time_since_last_fix > max_idle:
                logger.error(f"GPS data missing/invalid for over {max_idle}s. Initiating full service restart.")
                self._stop_services()
                self._start_services()
            
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
        
    manager._stop_services()
    logger.info("Service process finished.")
