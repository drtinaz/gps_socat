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
            # Keeping max_idle_time_seconds for the grace period
            'max_idle_time_seconds': config.getint('MONITORING', 'max_idle_time_seconds'),
            'watchdog_check_interval': config.getint('MONITORING', 'watchdog_check_interval'),
            # Removed unused DBus keys: dbus_service, dbus_object_path, dbus_property_name
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
        # MODIFIED: Tracks when an issue (process fail) was first detected. None when healthy.
        self.error_state_start_time = None 

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
        self._stop_services() # Clean state

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

        time.sleep(2) # Give time for TTY link to be created

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
        
        # Reset the error timer after a successful service start
        self.error_state_start_time = None 
        logger.info("Watchdog error timer reset due to successful service start.")

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

    # Removed the _get_dbus_satellite_count method
    
    def _watchdog_monitor(self):
        """GLib Timeout handler: Checks process status and uses max_idle_time_seconds as a grace period before restart."""
        
        # Check if processes are running (.poll() returns None if running)
        is_socat_dead = self.socat_process is None or self.socat_process.poll() is not None
        is_gps_dbus_dead = self.gps_dbus_process is None or self.gps_dbus_process.poll() is not None
        
        max_idle = self.config['max_idle_time_seconds']
        current_time = time.time()

        if is_socat_dead or is_gps_dbus_dead:
            # --- Services are dead/missing (Error State) ---
            
            if self.error_state_start_time is None:
                # FIRST FAILURE DETECTED: Start the timer and log the initial error
                self.error_state_start_time = current_time
                
                # Log only once:
                dead_proc = "socat" if is_socat_dead else "gps_dbus"
                if is_socat_dead and is_gps_dbus_dead:
                    dead_proc = "socat AND gps_dbus"
                    
                logger.error(f"Watchdog: {dead_proc} process found dead! Starting {max_idle}s restart grace timer.")
                
            else:
                # FAILURE PERSISTS: Check if the grace period has expired
                time_in_error_state = current_time - self.error_state_start_time
                
                if time_in_error_state > max_idle:
                    # GRACE PERIOD EXPIRED: Initiate restart
                    logger.critical(f"Watchdog: Service failure persisted for over {max_idle}s. Initiating full service RESTART.")
                    self._stop_services()
                    self._start_services()
                else:
                    # Waiting for grace period to expire
                    logger.warning(f"Watchdog: Service still down. Restart in {max_idle - time_in_error_state:.0f}s.")

        else:
            # --- Services are running (Healthy State) ---
            if self.error_state_start_time is not None:
                # We recovered naturally because error_state_start_time was set but processes are now running.
                time_in_error_state = current_time - self.error_state_start_time
                
                # Log the self-restoration event
                logger.info(f"Watchdog: Service self-restored after {time_in_error_state:.1f}s of downtime/grace period.")
                
                # Clear the error state timer
                self.error_state_start_time = None
            
            logger.debug("Watchdog: Services are running. Monitoring continues.")
            
        return True 

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
    
