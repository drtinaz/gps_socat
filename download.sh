#!/bin/bash

driver_path="/data/apps"
driver_name="gps_socat"

# check if /data/apps path exists
if [ ! -d "/data/apps" ]; then
    mkdir -p /data/apps
fi

echo ""
echo ""

# fetch version numbers for different versions
echo -n "Fetch current version numbers..."

# Option 1: latest stable release (tag marked 'latest' on GitHub)
latest_release_stable=$(curl -s https://api.github.com/repos/drtinaz/${driver_name}/releases/latest | grep "tag_name" | cut -d : -f 2,3 | tr -d "\ " | tr -d \" | tr -d \,)

# Option 2: latest beta/rc build (fetches latest tag with 'rc' or 'beta')
latest_release_beta=$(curl -s https://api.github.com/repos/drtinaz/${driver_name}/releases | sed -nE 's/.*"tag_name": "([^"]+(rc|beta))".*/\1/p' | head -n 1)


echo
PS3=$'\nSelect which version you want to install and enter the corresponding number: '

# create list of versions
version_list=(
    "latest stable release \"$latest_release_stable\""
    "beta version \"$latest_release_beta\""
    "quit"
)

select version in "${version_list[@]}"
do
    case $version in
        "latest stable release \"$latest_release_stable\"")
            break
            ;;
        "beta version \"$latest_release_beta\"")
            break
            ;;
        "quit")
            exit 0
            ;;
        *)
            echo "> Invalid option: $REPLY. Please enter a number!"
            ;;
    esac
done

echo "> Selected: $version"
echo ""


echo ""
if [ -d ${driver_path}/${driver_name} ]; then
    echo "Updating driver '$driver_name'..."
else
    echo "Installing driver '$driver_name'..."
fi


# change to temp folder
cd /tmp


# download driver
echo ""
echo "Downloading driver..."


## latest stable release (Option 1)
if [ "$version" = "latest stable release \"$latest_release_stable\"" ]; then
    # download latest release zipball URL from the GitHub API
    url=$(curl -s https://api.github.com/repos/drtinaz/${driver_name}/releases/latest | grep "zipball_url" | sed -n 's/.*"zipball_url": "\([^"]*\)".*/\1/p')
fi

## beta version (Option 2)
if [ "$version" = "beta version \"$latest_release_beta\"" ]; then
    # download specific beta tag zipball
    url="https://api.github.com/repos/drtinaz/${driver_name}/zipball/${latest_release_beta}"
fi

echo "Downloading from: $url"
wget -O /tmp/${driver_name}.zip "$url"

# check if download was successful
if [ ! -f /tmp/${driver_name}.zip ]; then
    echo ""
    echo "Download failed. Exiting..."
    exit 1
fi


# If updating: cleanup old folder
if [ -d /tmp/${driver_name}-master ]; then
    rm -rf /tmp/${driver_name}-master
fi


# unzip folder
echo "Unzipping driver..."
unzip ${driver_name}.zip

# Find and rename the extracted folder to be always the same
extracted_folder=$(find /tmp/ -maxdepth 1 -type d -name "*${driver_name}-*")

# Desired folder name
desired_folder="/tmp/${driver_name}-master"

# Check if the extracted folder exists and does not already have the desired name
if [ -n "$extracted_folder" ]; then
    if [ "$extracted_folder" != "$desired_folder" ]; then
        mv "$extracted_folder" "$desired_folder"
    else
        echo "Folder already has the desired name: $desired_folder"
    fi
else
    echo "Error: Could not find extracted folder. Exiting..."
    exit 1
fi


# If updating: backup existing config file
if [ -f ${driver_path}/${driver_name}/config.ini ]; then
    echo ""
    echo "Backing up existing config file..."
    mv ${driver_path}/${driver_name}/config.ini ${driver_path}/${driver_name}_config.ini
fi


# If updating: cleanup existing driver
if [ -d ${driver_path}/${driver_name} ]; then
    echo ""
    echo "Cleaning up existing driver..."
    rm -rf ${driver_path:?}/${driver_name}
fi


# copy files
echo ""
echo "Copying new driver files..."

cp -R /tmp/${driver_name}-master/ ${driver_path}/${driver_name}/

# remove temp files
echo ""
echo "Cleaning up temp files..."
rm -rf /tmp/${driver_name}.zip
rm -rf /tmp/${driver_name}-master


# If updating: restore existing config file
if [ -f ${driver_path}/${driver_name}_config.ini ]; then
    echo ""
    echo "Restoring existing config file..."
    mv ${driver_path}/${driver_name}_config.ini ${driver_path}/${driver_name}/config.ini
fi


# set permissions for files
echo ""
echo "Setting permissions for files..."
chmod 755 ${driver_path}/${driver_name}/${driver_name}.py
chmod 755 ${driver_path}/${driver_name}/install.sh
chmod 755 ${driver_path}/${driver_name}/restart.sh
chmod 755 ${driver_path}/${driver_name}/uninstall.sh
chmod 755 ${driver_path}/${driver_name}/service/run
chmod 755 ${driver_path}/${driver_name}/service/log/run
# chmod 755 ${driver_path}/${driver_name}/config.py


# copy default config file
if [ ! -f ${driver_path}/${driver_name}/config.ini ]; then
    echo ""
    echo ""
#    echo "First installation detected. Before completing the install"
#    echo "you must run the configuration script with the following command:"
#    echo "python ${driver_path}/${driver_name}/config.py"
#    echo ""
    echo "** Do not forget to edit the config file with your settings! **"
    echo "You can edit the config file with the following command:"
    echo "nano ${driver_path}/${driver_name}/config.ini"
    cp ${driver_path}/${driver_name}/config.sample.ini ${driver_path}/${driver_name}/config.ini
    echo ""
    echo "** Execute the install.sh script after you have edited the config.ini! **"
    echo "You can execute the install.sh script with the following command:"
    echo "bash ${driver_path}/${driver_name}/install.sh"
#    echo "or execute the restart.sh script if this is an update to an existing version:"
#    echo "bash ${driver_path}/${driver_name}/restart.sh"
    echo ""
 else
    echo "Restarting driver to apply new version..."
    sleep 5
    /bin/bash ${driver_path}/${driver_name}/restart.sh
fi


echo
echo "Done."
echo
echo
