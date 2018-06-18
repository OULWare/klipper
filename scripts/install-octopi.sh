#!/bin/bash
# This script installs Klipper on a Raspberry Pi machine running the
# OctoPi distribution.

PYTHONDIR="${HOME}/klippy-env"

# Step 1: Install system packages
install_packages()
{
    # Packages for python cffi
    PKGLIST="python-virtualenv libffi-dev python-opencv python-dev"
    # kconfig requirements
    PKGLIST="${PKGLIST} libncurses-dev"
    # hub-ctrl
    PKGLIST="${PKGLIST} libusb-dev"
    # AVR chip installation and building
    PKGLIST="${PKGLIST} avrdude gcc-avr binutils-avr avr-libc"
    # ARM chip installation and building
    PKGLIST="${PKGLIST} bossa-cli stm32flash libnewlib-arm-none-eabi"

    # Update system package info
    report_status "Running apt-get update..."
    sudo apt-get update

    # Install desired packages
    report_status "Installing packages..."
    sudo apt-get install --yes ${PKGLIST}
}

# Step 2: Create python virtual environment
create_virtualenv()
{
    report_status "Updating python virtual environment..."

    # Create virtualenv if it doesn't already exist
    [ ! -d ${PYTHONDIR} ] && virtualenv ${PYTHONDIR}

    # Install/update dependencies
    #${PYTHONDIR}/bin/pip install cffi==1.6.0 pyserial==3.2.1 greenlet==0.4.10 tornado==4.5
    ${PYTHONDIR}/bin/pip install -r ${SRCDIR}/requirements.txt
    ${PYTHONDIR}/bin/pip install RPi.GPIO
}

# Step 3: Install startup script
install_script()
{
    report_status "Installing system start script..."
    sudo cp "${SRCDIR}/scripts/klipper-start.sh" /etc/init.d/klipper
    sudo update-rc.d klipper defaults
}

# Step 4: Install startup script config
install_config()
{
    DEFAULTS_FILE=/etc/default/klipper
    [ -f $DEFAULTS_FILE ] && return

    report_status "Installing system start configuration..."
    sudo /bin/sh -c "cat > $DEFAULTS_FILE" <<EOF
# Configuration for /etc/init.d/klipper

KLIPPY_USER=$USER

KLIPPY_EXEC=${PYTHONDIR}/bin/python

KLIPPY_ARGS="${SRCDIR}/klippy/klippy.py ${HOME}/printer.cfg -l /tmp/klippy.log"

EOF
}

# Step 5: Start host software
start_software()
{
    report_status "Launching Klipper host software..."
    sudo /etc/init.d/klipper restart
}

# Helper functions
report_status()
{
    echo -e "\n\n###### $1"
}

verify_ready()
{
    if [ "$EUID" -eq 0 ]; then
        echo "This script must not run as root"
        exit -1
    fi
}

# Force script to exit if an error occurs
set -e

# Find SRCDIR from the pathname of this script
SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )"/.. && pwd )"

# Run installation steps defined above
verify_ready
install_packages
create_virtualenv
install_script
install_config
start_software
