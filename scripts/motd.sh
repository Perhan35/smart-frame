#!/bin/bash
# Smart Frame Dynamic MOTD Setup Script
# This script displays a nice banner and system info for your Smart Frame.

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
WHITE='\033[1;37m'
NC='\033[0m' # No Color

# ASCII Art
echo -e "${CYAN}"
echo "  _____                      _     ______                                "
echo " / ____|                    | |   |  ____|                               "
echo "| (___  _ __ ___   __ _ _ __| |_  | |__ _ __ __ _ _ __ ___   ___         "
echo " \___ \| '_ \` _ \ / _\` | '__| __| |  __| '__/ _\` | '_ \` _ \ / _ \    "
echo " ____) | | | | | | (_| | |  | |_  | |  | | | (_| | | | | | |  __/        "
echo "|_____/|_| |_| |_|\__,_|_|   \__| |_|  |_|  \__,_|_| |_| |_|\___|        "
echo -e "${NC}"

# System Info
USER_COUNT=$(who | wc -l)
UPTIME=$(uptime -p)
LOAD=$(cat /proc/loadavg | awk '{print $1}')
MEM_USED=$(free -m | awk 'NR==2{printf "%s/%s MB (%.2f%%)", $3,$2,$3*100/$2 }')
DISK_USED=$(df -h / | awk 'NR==2{printf "%s/%s (%s)", $3,$2,$5}')
IP_ADDR=$(hostname -I | awk '{print $1}')
CPU_TEMP=$(vcgencmd measure_temp | cut -d'=' -f2)

echo -e "${WHITE}--- System Information ---${NC}"
echo -e "${GREEN}Hostname:${NC} $(hostname)"
echo -e "${GREEN}IP Address:${NC} ${IP_ADDR}"
echo -e "${GREEN}Uptime:${NC} ${UPTIME}"
echo -e "${GREEN}CPU Temp:${NC} ${CPU_TEMP}"
echo -e "${GREEN}Load Info:${NC} ${LOAD}"
echo -e "${GREEN}Memory:${NC} ${MEM_USED}"
echo -e "${GREEN}Storage:${NC} ${DISK_USED}"
echo -e "${GREEN}Logged Users:${NC} ${USER_COUNT}"
echo ""
echo -e "${YELLOW}Welcome to your Smart Frame!${NC}"
echo "-------------------------------------------"
