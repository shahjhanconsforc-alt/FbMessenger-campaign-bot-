#!/bin/bash
# KVM host: forward two LAN ports to the FB-messenger-bot guest.
#   <host>:30502 -> guest:4300  (the console)
#   <host>:30903 -> guest:3001  (the web desktop, https)
# Installed as the fbbot-dnat.service systemd unit (see fbbot-dnat.service).
# Resolves the guest IP from libvirt so a changed lease still works.
HOST_IP=${HOST_IP:-192.168.100.191}
VM=FB-messenger-bot
MAC=52:54:00:fb:00:04
for i in $(seq 1 36); do
  G=$(virsh -q domifaddr $VM 2>/dev/null | awk '/ipv4/{print $4}' | cut -d/ -f1 | head -1)
  [ -z "$G" ] && G=$(virsh -q net-dhcp-leases default 2>/dev/null | awk -v m="$MAC" 'tolower($0)~tolower(m){print $5}' | cut -d/ -f1 | head -1)
  [ -n "$G" ] && break; sleep 5
done
[ -z "$G" ] && { logger "fbbot-dnat: no guest IP"; exit 1; }
fwd() {
  local HP=$1 GP=$2
  iptables -t nat -C PREROUTING -p tcp -d $HOST_IP --dport $HP -j DNAT --to-destination $G:$GP 2>/dev/null \
    || iptables -t nat -A PREROUTING -p tcp -d $HOST_IP --dport $HP -j DNAT --to-destination $G:$GP
  iptables -C FORWARD -p tcp -d $G --dport $GP -j ACCEPT 2>/dev/null \
    || iptables -I FORWARD 1 -p tcp -d $G --dport $GP -j ACCEPT
  iptables -t nat -C POSTROUTING -p tcp -d $G --dport $GP -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -p tcp -d $G --dport $GP -j MASQUERADE
  logger "fbbot-dnat: $HOST_IP:$HP -> $G:$GP"
}
fwd 30502 4300
fwd 30903 3001
