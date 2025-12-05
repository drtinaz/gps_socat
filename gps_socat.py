#!/usr/bin/env python3

import subprocess
import time
import os
import signal
import sys
import logging
import configparser
import dbus # Import dbus here
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# --- Configuration File Path ---
CONFIG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

# --- Global Logger Variable ---
logger = None

def setup_minimal_logging():
    """Sets up a minimal logger that only prints to standard error/output."""
    global logger
    logger = logging.getLogger(__name__)
    # Set level low enough to capture everything
    logger.setLevel(logging.DEBUG) 

    # Create console handler and set level to debug
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG)

    # Create a simple formatter
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)

    # Add the handler to the logger
    if not logger.handlers:
        logger.addHandler(ch)

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
            # Removed LOGGING keys to resolve the KeyError
        }
        return cfg
    except KeyError as e:
        # Using print here because logger is not yet fully configured
        print(f"ERROR: Missing configuration key in config.ini: {e}", file=sys.stderr)
        sys.exit(1)

class GpsServiceManager:
    
    def __init__(self):
        self.config = load_config()
        self.last_good_data_time = time.time()
        self.socat_proc = None
        self.gps_dbus_proc = None
        # NEW STATE VARIABLE: Tracks if the 'missing' message has been logged
        self.dbus_missing_logged = False
        
        self._start_services()
        
        # Set up the GLib timer for the watchdog monitor
        GLib.timeout_add_seconds(
            self.config['watchdog_check_interval'], 
            self._watchdog_monitor
        )

    def _start_services(self):
        """Starts socat and gps_dbus subprocesses."""
        
        # 1. Start socat
        socat_cmd = [
            self.config['socat_path'],
            f"TCP:{self.config['router_ip']}:{self.config['router_port']}",
            f"PTY,link={self.config['tty_device']},raw,echo=0"
        ]
        
        try:
            # Start socat process, redirecting stdout/stderr to files or discarding
            # Running without shell=True for better process management if possible
            self.socat_proc = subprocess.Popen(socat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info(f"Started socat process (PID: {self.socat_proc.pid}) streaming to {self.config['tty_device']}.")
            time.sleep(1) # Give time for TTY device to be created

        except FileNotFoundError:
            logger.error(f"Socat executable not found at {self.config['socat_path']}.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to start socat: {e}")
            sys.exit(1)
            
        # 2. Start gps-dbus
        # NOTE: -t 0 tells gps-dbus to keep retrying if the tty is not available
        gps_dbus_cmd = [
            self.config['gps_dbus_path'],
            '-s', self.config['tty_device'],
            '-b', str(self.config['baud_rate']),
            '-t', '0', 
            # '&' is removed here as it requires shell=True, which complicates process management.
            # We assume gps-dbus is a non-daemonizing service suitable for Popen.
        ]
        
        try:
            # Using Popen directly for better signal handling
            # If gps-dbus requires the environment set by a shell script, this may need adjustment.
            self.gps_dbus_proc = subprocess.Popen(gps_dbus_cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info(f"Started gps-dbus (PID: {self.gps_dbus_proc.pid}).")

        except FileNotFoundError:
            logger.error(f"gps-dbus executable not found at {self.config['gps_dbus_path']}.")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to start gps-dbus: {e}")
            # If gps-dbus fails to start, kill socat too for a clean exit
            self._stop_services() 
            sys.exit(1)
        
    def _stop_services(self):
        """Terminates socat and gps_dbus processes."""
        
        logger.info("Stopping services.")
        
        # 1. Terminate gps-dbus
        if self.gps_dbus_proc and self.gps_dbus_proc.poll() is None:
            # Use terminate and then kill if needed
            self.gps_dbus_proc.terminate()
            try:
                self.gps_dbus_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.gps_dbus_proc.kill()
            logger.info("gps-dbus process terminated.")
            
        # 2. Terminate socat
        if self.socat_proc and self.socat_proc.poll() is None:
            self.socat_proc.terminate()
            try:
                self.socat_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.socat_proc.kill()
            logger.info("socat process terminated.")

        # Clean up the tty link if it still exists
        if os.path.islink(self.config['tty_device']):
            os.unlink(self.config['tty_device'])
            logger.debug(f"Removed tty link {self.config['tty_device']}")
        
        # Give the system a brief moment to stabilize after stopping
        time.sleep(1) 

    def _check_dbus_service_status(self):
        """Checks if the gps-dbus service name is currently registered on the D-Bus."""
        try:
            # Get a connection to the system bus
            bus = dbus.SystemBus() 
            # Check if the configured service name is in the list of active names
            is_running = self.config['dbus_service'] in bus.list_names()
            return is_running
                
        except Exception as e:
            # Failsafe: Log error if D-Bus communication itself fails
            logger.error(f"Failed to communicate with D-Bus while checking status: {e}")
            return False

    def _watchdog_monitor(self):
        """
        Monitors the gps-dbus service existence on D-Bus. 
        Restarts services if missing for longer than max_idle_time_seconds.
        """
        
        service_is_active = self._check_dbus_service_status() 
        max_idle = self.config['max_idle_time_seconds']
        
        if service_is_active:
            # --- A) Service is Active ---
            
            # Check if we were previously in a missing state (flag is True)
            if self.dbus_missing_logged:
                # Log restoration message, clear flag, and reset timer
                logger.info("GPS DBus service restored. Timer reset.")
                self.dbus_missing_logged = False
                
            # Reset the timer (for normal operation or after restoration)
            self.last_good_data_time = time.time()
            logger.debug("GPS DBus service is active. Monitoring continues.")
            
        else:
            # --- B) Service is Missing ---
            time_since_last_fix = time.time() - self.last_good_data_time
            
            # Log the missing message ONLY ONCE
            if not self.dbus_missing_logged:
                logger.warning(
                    f"GPS DBus service is MISSING. Starting idle timer. Time since last active: {time_since_last_fix:.0f}s."
                ) 
                self.dbus_missing_logged = True # Set the flag so it doesn't log again
                
            else:
                logger.debug(
                    f"GPS DBus service is still missing. Time since last active: {time_since_last_fix:.0f}s."
                )

            # Check for timeout and initiate restart
            if time_since_last_fix > max_idle:
                logger.error(f"GPS service missing for over {max_idle}s. Initiating full service restart.")
                # The flag remains True; it will be reset only after successful service restoration
                self._stop_services()
                self._start_services()
            
        return True 

def signal_handler(sig, frame):
    """Gracefully handles termination signals (TERM, INT)."""
    logger.info(f"Received signal {sig}. Stopping MainLoop.")
    GLib.MainLoop().quit()
    
# --- Main Execution ---
if __name__ == "__main__":
    
    setup_minimal_logging() # Use the minimal logging setup
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    DBusGMainLoop(set_as_default=True)
    manager = GpsServiceManager() 
    
    mainloop = GLib.MainLoop()
    try:
        mainloop.run()
    except Exception as e:
        logger.error(f"Main loop encountered an unhandled exception: {e}")
    finally:
        manager._stop_services()
        logger.info("Application shut down.")
        
