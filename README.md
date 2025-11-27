this installs a service on Venus os that installs socat and receives gps data from a network connected source such as a router, and publishes it to the dbus

*************** INSTALL USING SSH ******************
1. Download the package:
```
wget -O /tmp/download.sh https://raw.githubusercontent.com/drtinaz/gps_socat/master/download.sh
bash /tmp/download.sh
```

2. edit the config file and set the ip address of the socat source:
```
nano /data/apps/gps_socat/config.ini
```

3. run the install script:
```
bash /data/apps/gps_socat/install.sh
```

to uninstall:
```
bash /data/apps/gps_socat/uninstall.sh
```