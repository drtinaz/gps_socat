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
import dbus
from dbus.exceptions import DBusException

# --- Configuration File Path ---
CONFIG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

# --- Hardcoded Settings ---
UTC_CHECK_INTERVAL = 30
NO_UTC_TIMEOUT = 120
STARTUP_WAIT_SECONDS = 5
STARTUP_MAX_WAIT = 30

# --- DBus paths for gps_dbus service ---
GPS_DBUS_SERVICE_PREFIX = "com.victronenergy.gps"
GPS_DBUS_OBJECT_PATH = "/UtcTime"
GPS_DBUS_INTERFACE = "com.victronenergy.BusItem"

# --- Global Logger Variable ---
logger = None

def load_config():
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
            'baud_rate': config.getint('CONNECTION', 'baud_rate'),
            'gps_dbus_path': config['PATHS']['gps_dbus_path'],
            'socat_path': config['PATHS']['socat_path'],
            'initial_backoff_seconds': config.getint('BACKOFF', 'initial_backoff_seconds'),
            'max_backoff_seconds': config.getint('BACKOFF', 'max_backoff_seconds'),
        }
        return cfg
    except (KeyError, configparser.NoSectionError) as e:
        print(f"ERROR: Missing configuration: {e}", file=sys.stderr)
        sys.exit(1)

def setup_logging():
    global logger
    logger = logging.getLogger('GpsService')
    logger.setLevel(logging.INFO)
    
    if logger.hasHandlers():
        logger.handlers.clear()
    
    stream_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    
    logger.info("=" * 60)
    logger.info("GPS SERVICE MANAGER INITIALIZING")
    logger.info("=" * 60)
    return logger

class GpsServiceManager:
    def __init__(self):
        self.config = load_config()
        self.socat_process = None
        self.gps_dbus_process = None
        
        # Dynamically determine DBus service name from config TTY device
        self.tty_device_name = os.path.basename(self.config['tty_device'])
        self.dbus_service_name = f"{GPS_DBUS_SERVICE_PREFIX}.ve_{self.tty_device_name}"
        self.dbus_object_path = GPS_DBUS_OBJECT_PATH
        
        # Backoff tracking
        self.consecutive_failures = 0
        self.current_backoff_seconds = self.config['initial_backoff_seconds']
        self.restart_timer_id = None
        self.waiting_for_restart = False
        
        # UTC monitoring
        self.last_utc_time = None
        self._wait_start_time = None
        self.watchdog_id = None
        self.startup_verified = False
        
        # DBus
        self.bus = dbus.SystemBus()
        
        # Setup NameOwnerChanged monitoring for GPS service disappearance
        self.bus.add_signal_receiver(
            self._on_gps_service_disappeared,
            bus_name="org.freedesktop.DBus",
            dbus_interface="org.freedesktop.DBus",
            signal_name="NameOwnerChanged",
            arg0=self.dbus_service_name
        )
        
        # Setup signal handlers for process crashes
        self._setup_signal_handlers()
        
        logger.info("Configuration loaded:")
        logger.info(f"  Router: {self.config['router_ip']}:{self.config['router_port']}")
        logger.info(f"  TTY Device: {self.config['tty_device']}")
        logger.info(f"  TTY Device Name: {self.tty_device_name}")
        logger.info(f"  DBus Service: {self.dbus_service_name}")
        logger.info(f"  DBus Object Path: {self.dbus_object_path}")
        logger.info(f"  DBus Interface: {GPS_DBUS_INTERFACE}")
        logger.info(f"  Backoff: initial={self.config['initial_backoff_seconds']}s, max={self.config['max_backoff_seconds']}s")
        logger.info(f"  UTC Check Interval: {UTC_CHECK_INTERVAL}s")
        logger.info(f"  No-UTC Timeout: {NO_UTC_TIMEOUT}s")
        
        if not self._install_socat():
            sys.exit(1)

        self._start_services()
        
    def _setup_signal_handlers(self):
        """Setup signal handlers for child process monitoring."""
        signal.signal(signal.SIGCHLD, self._handle_sigchld)
        logger.info("Signal handlers registered (SIGCHLD)")
    
    def _on_gps_service_disappeared(self, name, old_owner, new_owner):
        """Called when GPS DBus service disappears."""
        if old_owner and not new_owner:
            logger.error(f"!!! GPS DBUS SERVICE DISAPPEARED !!!")
            logger.error(f"  Service {name} is no longer available")
    
    def _handle_sigchld(self, signum, frame):
        """Called immediately when any child process dies."""
        GLib.idle_add(self._check_crashed_processes)
        
    def _check_crashed_processes(self):
        """Check which process(es) crashed and trigger restart."""
        is_socat_alive = self.socat_process and self.socat_process.poll() is None
        is_gps_dbus_alive = self.gps_dbus_process and self.gps_dbus_process.poll() is None
        
        if not is_socat_alive or not is_gps_dbus_alive:
            dead_proc = []
            if not is_socat_alive:
                dead_proc.append("socat")
            if not is_gps_dbus_alive:
                dead_proc.append("gps_dbus")
            
            logger.error(f"!!! PROCESS CRASH DETECTED: {', '.join(dead_proc)}")
            self._immediate_restart()
        
        return False
    
    def _read_utc_time(self):
        """Read current UTC time using com.victronenergy.BusItem.GetValue."""
        try:
            # Check if service exists first
            if not self.bus.name_has_owner(self.dbus_service_name):
                return None
            
            # Use the BusItem interface to get the value
            obj = self.bus.get_object(self.dbus_service_name, self.dbus_object_path)
            bus_item = dbus.Interface(obj, GPS_DBUS_INTERFACE)
            utc_time = bus_item.GetValue()
            
            if utc_time is not None and utc_time != "":
                utc_str = str(utc_time)
                if utc_str.lower() != "null":
                    return utc_str
            return None
        except DBusException as e:
            logger.debug(f"Failed to read UTC time: {e}")
            return None
        except Exception as e:
            logger.debug(f"Unexpected error reading UTC time: {e}")
            return None
    
    def _wait_for_data_stream(self):
        """Wait for first valid data stream after startup."""
        logger.info("Waiting for GPS data stream...")
        start_time = time.time()
        
        while time.time() - start_time < STARTUP_MAX_WAIT:
            utc_time = self._read_utc_time()
            if utc_time is not None:
                logger.info("=" * 60)
                logger.info(f"✓✓✓ SUCCESSFUL STARTUP ✓✓✓")
                logger.info(f"  Data stream verified! UTC time: {utc_time}")
                logger.info(f"  GPS service is functioning correctly")
                logger.info(f"  Monitoring active with {UTC_CHECK_INTERVAL}s checks")
                logger.info("=" * 60)
                self.last_utc_time = utc_time
                self.startup_verified = True
                return True
            
            time.sleep(2)
        
        logger.warning(f"No data stream detected after {STARTUP_MAX_WAIT}s")
        return False
    
    def _start_watchdog(self):
        """Start the periodic watchdog timer."""
        if self.watchdog_id is not None:
            return
        
        self.watchdog_id = GLib.timeout_add_seconds(UTC_CHECK_INTERVAL, self._watchdog_check)
        logger.info(f"Watchdog started - checking every {UTC_CHECK_INTERVAL} seconds")
    
    def _stop_watchdog(self):
        """Stop the periodic watchdog timer."""
        if self.watchdog_id is not None:
            GLib.source_remove(self.watchdog_id)
            self.watchdog_id = None
            logger.debug("Watchdog stopped")
    
    def _watchdog_check(self):
        """Periodic check for UTC staleness and service availability."""
        if self.waiting_for_restart:
            return True
        
        if not self._are_services_running():
            logger.debug("Services not running, skipping check")
            return True
        
        if not self.bus.name_has_owner(self.dbus_service_name):
            logger.error(f"!!! GPS DBUS SERVICE UNAVAILABLE !!!")
            logger.error(f"  Service {self.dbus_service_name} is not registered on DBus")
            self._immediate_restart()
            return True
        
        current_utc = self._read_utc_time()
        
        if current_utc is not None:
            if self.last_utc_time is not None:
                if current_utc != self.last_utc_time:
                    logger.debug(f"Data flow OK - UTC: {current_utc}")
                    self.last_utc_time = current_utc
                    self._wait_start_time = None
                    
                    if self.consecutive_failures > 0:
                        self._reset_backoff()
                else:
                    logger.warning(f"⚠ Data flow STALE - UTC unchanged: {current_utc}")
                    logger.error(f"!!! DATA FLOW FAILURE DETECTED !!!")
                    self._immediate_restart()
            else:
                logger.info(f"✓ Data flow restored - UTC: {current_utc}")
                self.last_utc_time = current_utc
                self._wait_start_time = None
                
                if self.consecutive_failures > 0:
                    self._reset_backoff()
        else:
            if self.last_utc_time is not None:
                logger.error(f"!!! DATA FLOW LOST !!!")
                logger.error(f"  UTC became invalid (was: {self.last_utc_time})")
                self._immediate_restart()
            else:
                if self._wait_start_time is None:
                    self._wait_start_time = time.time()
                    logger.info(f"Waiting for first valid UTC data from GPS...")
                    logger.info(f"  Timeout: {NO_UTC_TIMEOUT}s")
                else:
                    wait_duration = time.time() - self._wait_start_time
                    if wait_duration > NO_UTC_TIMEOUT:
                        logger.error(f"!!! STARTUP TIMEOUT !!!")
                        logger.error(f"  No valid UTC data after {wait_duration:.0f} seconds")
                        self._immediate_restart()
                        self._wait_start_time = None
        
        return True
    
    def _are_services_running(self):
        """Check if both services are currently running."""
        socat_running = self.socat_process and self.socat_process.poll() is None
        gps_running = self.gps_dbus_process and self.gps_dbus_process.poll() is None
        return socat_running and gps_running
    
    def _immediate_restart(self):
        """Immediately restart services and start backoff waiting period."""
        if self.waiting_for_restart:
            return
        
        self._stop_watchdog()
        self._calculate_next_backoff()
        
        logger.warning("=" * 60)
        logger.warning(f"!!! EXECUTING SERVICE RESTART !!!")
        logger.warning(f"  Failure count: {self.consecutive_failures}")
        logger.warning(f"  Backoff delay: {self.current_backoff_seconds}s")
        logger.warning("=" * 60)
        
        self._stop_services()
        success = self._start_services()
        
        if success:
            self.last_utc_time = None
            self._wait_start_time = None
            self.startup_verified = False
            self.waiting_for_restart = True
            self._cancel_restart_timer()
            self.restart_timer_id = GLib.timeout_add_seconds(self.current_backoff_seconds, self._end_waiting_period)
            logger.info(f"✓ Restart completed, waiting {self.current_backoff_seconds}s")
        else:
            logger.error(f"!!! RESTART FAILED - retrying !!!")
            GLib.idle_add(self._immediate_restart)
    
    def _end_waiting_period(self):
        """End waiting period after restart."""
        self.restart_timer_id = None
        self.waiting_for_restart = False
        
        logger.info("=" * 60)
        logger.info(f"✓ Waiting period ended - resuming checks")
        logger.info("=" * 60)
        
        self._start_watchdog()
        self._watchdog_check()
        
        return False
    
    def _calculate_next_backoff(self):
        """Calculate next backoff value (exponential capped at max)."""
        old_backoff = self.current_backoff_seconds
        self.consecutive_failures += 1
        self.current_backoff_seconds = min(
            self.config['initial_backoff_seconds'] * (2 ** (self.consecutive_failures - 1)),
            self.config['max_backoff_seconds']
        )
        logger.info(f"Backoff: failure #{self.consecutive_failures}, {old_backoff}s -> {self.current_backoff_seconds}s")
    
    def _reset_backoff(self):
        """Reset backoff after successful data flow."""
        if self.consecutive_failures > 0:
            logger.info(f"✓✓✓ DATA FLOW RESTORED - Resetting backoff to {self.config['initial_backoff_seconds']}s")
            self.consecutive_failures = 0
            self.current_backoff_seconds = self.config['initial_backoff_seconds']
    
    def _cancel_restart_timer(self):
        """Cancel any pending timer."""
        if self.restart_timer_id is not None:
            GLib.source_remove(self.restart_timer_id)
            self.restart_timer_id = None
    
    def _install_socat(self):
        """Install socat if missing."""
        if os.path.exists(self.config['socat_path']):
            logger.info(f"✓ socat found at {self.config['socat_path']}")
            return True
        
        logger.warning(f"socat not found, installing...")
        try:
            subprocess.run(["opkg", "update"], check=True, capture_output=True)
            subprocess.run(["opkg", "install", "socat"], check=True, capture_output=True)
            logger.info("✓ socat installed")
            return True
        except Exception as e:
            logger.error(f"Failed to install socat: {e}")
            return False

    def _start_services(self):
        """Start socat and gps_dbus processes."""
        logger.info("-" * 60)
        logger.info("STARTING SERVICES")
        logger.info("-" * 60)
        
        self._stop_services()

        # Start socat
        socat_cmd = [
            self.config['socat_path'],
            f"TCP:{self.config['router_ip']}:{self.config['router_port']}",
            f"pty,link={self.config['tty_device']},raw,nonblock,echo=0,b{self.config['baud_rate']}"
        ]
        logger.info(f"Starting socat...")
        try:
            self.socat_process = subprocess.Popen(socat_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            logger.info(f"  ✓ socat started (PID: {self.socat_process.pid})")
        except Exception as e:
            logger.error(f"  ✗ Failed to start socat: {e}")
            return False

        logger.info(f"Waiting 2 seconds for TTY device to be created...")
        time.sleep(2)
        
        if os.path.exists(self.config['tty_device']):
            logger.info(f"  ✓ TTY device {self.config['tty_device']} created")
        else:
            logger.warning(f"  ⚠ TTY device {self.config['tty_device']} not found")

        # Start gps_dbus
        gps_dbus_cmd = [
            self.config['gps_dbus_path'],
            "-s", self.config['tty_device'],
            "-b", str(self.config['baud_rate']),
            "-t", "0"
        ]
        logger.info(f"Starting gps_dbus...")
        try:
            self.gps_dbus_process = subprocess.Popen(gps_dbus_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            logger.info(f"  ✓ gps_dbus started (PID: {self.gps_dbus_process.pid})")
        except Exception as e:
            logger.error(f"  ✗ Failed to start gps_dbus: {e}")
            self._stop_services()
            return False
            
        logger.info("-" * 60)
        logger.info(f"SERVICES STARTED")
        logger.info(f"  socat PID: {self.socat_process.pid}")
        logger.info(f"  gps_dbus PID: {self.gps_dbus_process.pid}")
        logger.info(f"  TTY Device: {self.config['tty_device']}")
        logger.info("-" * 60)
        
        # Wait for DBus service to register
        logger.info(f"Waiting {STARTUP_WAIT_SECONDS} seconds for GPS service to register on DBus...")
        time.sleep(STARTUP_WAIT_SECONDS)
        
        # Verify data stream
        if self._wait_for_data_stream():
            self._start_watchdog()
        else:
            logger.warning("No data stream detected during startup - will retry via watchdog")
            self._start_watchdog()
            self._wait_start_time = time.time()
        
        return True

    def _stop_services(self):
        """Stop all services."""
        if self.socat_process is None and self.gps_dbus_process is None:
            return
        
        self._stop_watchdog()
            
        logger.info("Stopping services...")
        for proc_name, proc in [("gps_dbus", self.gps_dbus_process), ("socat", self.socat_process)]:
            if proc and proc.poll() is None:
                logger.info(f"  Stopping {proc_name} (PID: {proc.pid})")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                    logger.info(f"  ✓ {proc_name} stopped")
                except subprocess.TimeoutExpired:
                    proc.kill()
                    logger.warning(f"  ⚠ {proc_name} had to be killed")
                    
        self.socat_process = None
        self.gps_dbus_process = None
        logger.info("All services stopped")

def signal_handler(sig, frame):
    logger.info(f"Received signal {sig}, shutting down...")
    GLib.MainLoop().quit()
    
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
        logger.error(f"Main loop error: {e}")
    manager._stop_services()
    logger.info("=" * 60)
    logger.info("GPS Service Manager Stopped")
    logger.info("=" * 60)
