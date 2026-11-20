# services/ztp_provisioning_service.py

import logging
import re
import ipaddress
import subprocess
import json
import os
import time
from typing import Dict, Any, Tuple
from fastapi import HTTPException
from meraki.exceptions import APIError

from core.config import get_settings

logger = logging.getLogger("ZTP_Orchestrator")

class ZtpProvisioningService:
    def __init__(self, meraki_provider, ise_client):
        settings = get_settings()
        self.meraki = meraki_provider
        self.ise = ise_client
        raw_vlan = os.getenv("ISE_NDG_TARGET_VLAN_ID")
        self.target_vlan = int(raw_vlan) if raw_vlan is not None else settings.ISE_NDG_TARGET_VLAN_ID

    def run_ztp_pipeline(self, payload: Any, progress_callback=None) -> Dict[str, Any]:
        def update_progress(pct: int, msg: str):
            if progress_callback:
                progress_callback(pct, msg)
            logger.info(f"ZTP Pipeline: {msg}")

        results = {
            "network_id": None,
            "gateway_ip": None,
            "subnet": None,
            "dhcp_status": "Pending",
            "ise_status": "Pending",
            "mode": "Greenfield"
        }

        created_network_id = None
        computed_device_name = ""
        
        # Extract the last 4 characters of the serial number safely[cite: 6]
        clean_serial = payload.serial_number.replace("-", "").strip()
        last_4_sn = clean_serial[-4:] if len(clean_serial) >= 4 else clean_serial

        try:
            # -----------------------------------------------------------------
            # STAGE 1: Network Assessment & Creation/Discovery
            # -----------------------------------------------------------------
            update_progress(5, "Assessing target organization networks (bypassing cache)...")
            
            # Call the Meraki SDK directly to bypass the 30-minute local cache
            existing_networks = self.meraki.dashboard.organizations.getOrganizationNetworks(self.meraki.organization_id)
            
            # NEW: Strip all dashes, underscores, and spaces to perfectly match Meraki's database rules
            def normalize_name(name: str) -> str:
                return re.sub(r'[^a-z0-9]', '', name.lower())

            target_net = next((n for n in existing_networks if normalize_name(n["name"]) == normalize_name(payload.network_name)), None)

            if target_net:
                results["mode"] = "Replacement/RMA"
                net_id = target_net["id"]
                results["network_id"] = net_id
                update_progress(15, f"Existing network discovered ('{target_net['name']}'). Switching to RMA/Replacement mode.")
            else:
                # Dynamically query the target template to see what network types it requires
                update_progress(8, "Resolving target template infrastructure requirements...")
                templates = self.meraki.dashboard.organizations.getOrganizationConfigTemplates(self.meraki.organization_id)
                target_template = next((t for t in templates if t["id"] == payload.template_id), None)
                
                if not target_template:
                    raise Exception(f"Configuration Template ID '{payload.template_id}' could not be located.")
                
                # Extract the exact product types from the template (fallback to appliance just in case)
                template_product_types = target_template.get("productTypes", ["appliance"])
                
                update_progress(10, f"Creating new Meraki network matching template types: {template_product_types}...")
                
                # Build the network using the exact types required by the template
                new_net = self.meraki.dashboard.organizations.createOrganizationNetwork(
                    self.meraki.organization_id,
                    name=payload.network_name,
                    productTypes=template_product_types,
                    timeZone="America/Chicago" 
                )
                net_id = new_net["id"]
                created_network_id = net_id
                results["network_id"] = net_id
                
                update_progress(12, "Allowing Meraki cloud database shards to synchronize...")
                time.sleep(5)

            # -----------------------------------------------------------------
            # STAGE 2: Device Claiming & Error Circuit-Breaking
            # -----------------------------------------------------------------
            update_progress(25, f"Validating and claiming hardware serial [{payload.serial_number}]...")
            try:
                self.meraki.dashboard.networks.claimNetworkDevices(
                    net_id, 
                    serials=[payload.serial_number]
                )
            except APIError as e:
                err_msg = str(e)
                if created_network_id:
                    logger.warning(f"ZTP Claim failed. Rolling back and deleting orphaned network ID: {created_network_id}")
                    try:
                        self.meraki.dashboard.networks.deleteNetwork(created_network_id)
                    except Exception as rb_err:
                        logger.error(f"Failed to delete orphaned network during rollback: {rb_err}")

                if "invalid serial" in err_msg.lower() or "not found" in err_msg.lower():
                    raise Exception(f"The Serial Number '{payload.serial_number}' is invalid or does not exist in your Meraki inventory.")
                elif "already claimed" in err_msg.lower():
                    raise Exception(f"The Serial Number '{payload.serial_number}' is already claimed by another network or organization.")
                else:
                    raise Exception(f"Meraki Inventory Claim Error: {err_msg}")
                    
            # -----------------------------------------------------------------
            # STAGE 2B: Hardware Query & Naming Updates
            # -----------------------------------------------------------------
            update_progress(28, "Querying hardware model details from Meraki cloud...")
            
            device_info = {}
            for attempt in range(4):
                time.sleep(4)
                try:
                    device_info = self.meraki.dashboard.devices.getDevice(payload.serial_number)
                    break
                except Exception:
                    pass
            
            actual_model = device_info.get("model", "Unknown")
            computed_device_name = f"{payload.network_name}_{actual_model}_{last_4_sn}"
            
            update_progress(30, f"Updating device name to '{computed_device_name}' and setting geographic address...")
            
            # Retry loop for updateDevice due to write-lock eventual consistency
            for attempt in range(4):
                try:
                    self.meraki.dashboard.devices.updateDevice(
                        payload.serial_number,
                        name=computed_device_name,
                        address=payload.physical_address,
                        moveMarker=True
                    )
                    break
                except Exception as upd_err:
                    if attempt == 3:
                        raise Exception(f"Device successfully claimed, but Meraki rejected the name update (Eventual Consistency): {str(upd_err)}")
                    time.sleep(4)

            # -----------------------------------------------------------------
            # STAGE 3: Bind to Template
            # -----------------------------------------------------------------
            if results["mode"] == "Greenfield":
                update_progress(40, "Binding network to standard compliance template...")
                self.meraki.dashboard.networks.bindNetwork(
                    net_id, 
                    configTemplateId=payload.template_id
                )
            else:
                update_progress(40, "Network already bound to template. Verifying baseline inheritance...")

            # -----------------------------------------------------------------
            # STAGE 4: Extract Dynamically Assigned IP/Subnet
            # -----------------------------------------------------------------
            update_progress(60, f"Extracting dynamic VLAN {self.target_vlan} IP allocation footprint...")
            vlan_data = self._get_vlan_with_retry(net_id)
            if not vlan_data:
                raise Exception(f"VLAN {self.target_vlan} could not be resolved. Ensure it is defined on the template.")

            gateway_ip = vlan_data["applianceIp"]
            subnet_cidr = vlan_data["subnet"]
            results["gateway_ip"] = gateway_ip
            results["subnet"] = subnet_cidr
            
            network_obj = ipaddress.IPv4Network(subnet_cidr, strict=False)
            network_address = str(network_obj.network_address)
            prefix_length = network_obj.prefixlen

            # -----------------------------------------------------------------
            # STAGE 5: Microsoft DHCP Scope Verification/Creation
            # -----------------------------------------------------------------
            update_progress(75, f"Provisioning Microsoft DHCP Scope for scope block {subnet_cidr}...")
            try:
                self._provision_dhcp_scope(
                    network_address=network_address, 
                    prefix_length=prefix_length, 
                    scope_name=f"{payload.network_name}_VLAN{self.target_vlan}"
                )
                results["dhcp_status"] = "Success"
            except Exception as dhcp_err:
                if "already exists" in str(dhcp_err).lower() or results["mode"] == "Replacement/RMA":
                    logger.info("DHCP Scope already exists on target server. Retaining existing footprint.")
                    results["dhcp_status"] = "Success (Retained)"
                else:
                    raise dhcp_err

            # -----------------------------------------------------------------
            # STAGE 6: Cisco ISE RADIUS Reconciliation
            # -----------------------------------------------------------------
            update_progress(90, f"Reconciling AAA tracking profiles inside Cisco ISE for gateway {gateway_ip}...")
            
            # FIXED: Pass our exactly formatted string to keep ISE and Meraki fully synchronized
            ise_msg = self._provision_or_update_ise_device(payload, gateway_ip, computed_device_name)
            results["ise_status"] = ise_msg

            update_progress(100, f"Zero-Touch Provisioning Pipeline Successfully Executed [{results['mode']} Mode].")
            return results

        except Exception as exc:
            logger.exception("ZT Provisioning Execution Failure")
            raise HTTPException(status_code=500, detail=str(exc))

    def _get_vlan_with_retry(self, net_id: str, retries: int = 4) -> Dict[str, Any]:
        for attempt in range(retries):
            time.sleep(3)
            try:
                vlans = self.meraki.dashboard.appliance.getNetworkApplianceVlans(net_id)
                for v in vlans:
                    if v["id"] == self.target_vlan:
                        return v
            except Exception:
                pass
        return None

    def _provision_dhcp_scope(self, network_address: str, prefix_length: int, scope_name: str):
        ps_script_path = os.path.abspath("scripts/Provision-ZtpDhcpScope.ps1")
        if not os.path.exists(ps_script_path):
            raise Exception("DHCP orchestration script missing from host environment.")

        settings = get_settings()
        primary_server = (os.getenv("ZTP_DHCP_PRIMARY_SERVER") or settings.ZTP_DHCP_PRIMARY_SERVER).strip()
        partner_server = (os.getenv("ZTP_DHCP_PARTNER_SERVER") or settings.ZTP_DHCP_PARTNER_SERVER).strip()
        failover_name = (
            os.getenv("ZTP_DHCP_FAILOVER_RELATIONSHIP") or settings.ZTP_DHCP_FAILOVER_RELATIONSHIP
        ).strip()
        raw_dns_servers = os.getenv("ZTP_DHCP_DNS_SERVERS")
        dns_servers = (
            [s.strip() for s in raw_dns_servers.split(",") if s.strip()]
            if raw_dns_servers is not None
            else settings.ztp_dhcp_dns_servers_list
        )
        dns_domain = (os.getenv("ZTP_DHCP_DNS_DOMAIN") or settings.effective_ztp_dhcp_dns_domain).strip()
        raw_lease_days = os.getenv("ZTP_DHCP_LEASE_DAYS")
        lease_days = int(raw_lease_days) if raw_lease_days is not None else settings.ZTP_DHCP_LEASE_DAYS

        command = [
            "powershell.exe", "-ExecutionPolicy", "Bypass", "-File", ps_script_path,
            "-NetworkAddress", network_address,
            "-PrefixLength", str(prefix_length),
            "-ScopeName", scope_name,
            "-PrimaryServer", primary_server,
            "-PartnerServer", partner_server,
            "-DnsDomain", dns_domain,
            "-LeaseDays", str(lease_days),
        ]
        if failover_name:
            command.extend(["-FailoverRelationshipName", failover_name])
        if dns_servers:
            command.extend(["-DnsServers", ",".join(dns_servers)])

        process = subprocess.run(command, capture_output=True, text=True, timeout=45)
        if process.returncode != 0:
            raise Exception(f"DHCP Script Error: {process.stderr or process.stdout}")

    def _provision_or_update_ise_device(self, payload: Any, ip_address: str, computed_device_name: str) -> str:
        settings = get_settings()
        # Use the exact naming scheme fetched directly from Meraki
        raw_ise_name = computed_device_name
        
        # Clean naming characters to satisfy Cisco ERS schema constraints safely
        sanitized_name = re.sub(r'[\s\-]+', '_', raw_ise_name.strip())
        sanitized_name = re.sub(r'[^A-Za-z0-9\.\-_]', '', sanitized_name)
        sanitized_name = re.sub(r'_{2,}', '_', sanitized_name)

        default_location_ndg = (
            os.getenv("ISE_DEFAULT_LOCATION_NDG") or settings.ISE_DEFAULT_LOCATION_NDG
        ).strip()
        radius_shared_secret = (
            os.getenv("RADIUS_SHARED_SECRET") or settings.RADIUS_SHARED_SECRET
        ).strip()

        ndg_list = [
            default_location_ndg,
            "IPSEC#Is IPSEC Device#No",
            f"Device Type#All Device Types#Meraki#{payload.gateway_type}",
            f"Designation#Designation#{payload.gateway_designation}",
            f"Deployment Stages#Deployment Stages#{payload.deployment_stage}"
        ]

        ise_payload = {
            "NetworkDevice": {
                "name": sanitized_name,
                "description": f"ZTP Dashboard Managed. Address Map Target: {payload.physical_address}",
                "profileName": "Meraki_NoCoA",
                "NetworkDeviceIPList": [{"ipaddress": ip_address, "mask": 32}],
                "NetworkDeviceGroupList": ndg_list,
                "authenticationSettings": {
                    "networkProtocol": "RADIUS",
                    "radiusSharedSecret": radius_shared_secret
                }
            }
        }

        # Step 1: Pre-flight check for matching Name
        try:
            # FIXED: Bypassing missing wrapper methods by using the internal request pipeline directly
            lookup_url = f"{self.ise.base_url}/name/{sanitized_name}"
            res = self.ise._request("GET", lookup_url)
            existing_by_name = res.json() if hasattr(res, "json") else res
            
            if existing_by_name and "NetworkDevice" in existing_by_name:
                dev_id = existing_by_name["NetworkDevice"]["id"]
                logger.info(f"Name collision match found for '{sanitized_name}'. Updating existing record ID: {dev_id}")
                
                # Bulletproof PUT update 
                update_url = f"{self.ise.base_url}/{dev_id}"
                self.ise._request("PUT", update_url, json=ise_payload)
                return "Success (Updated Existing Name)"
        except Exception:
            pass # No name collision, move to creation phase

        # Step 2: Attempt standard entry creation
        try:
            self.ise.create_network_device(ise_payload)
            return "Success (Created)"
        except Exception as err:
            err_str = str(err)
            
            # Catch Name Overlaps
            if "already exists" in err_str.lower() or "409" in err_str or "name uniquely identifies" in err_str.lower():
                raise Exception(f"Cisco ISE API Rejection: Device Name collision detected but pre-flight check failed. {err_str}")

            # Catch IP Overlaps & Extract Legacy Name
            elif "overlapping ip address" in err_str.lower() or "400" in err_str:
                logger.warning(f"IP Overlap encountered for gateway IP {ip_address}. Reconciling conflict...")
                
                try:
                    # Extract the exact conflicting device name directly from ISE's error payload
                    match = re.search(r'Overlapping IP Address in other device:\s*([^"]+)', err_str)
                    
                    if match:
                        conflicting_name = match.group(1).strip()
                        logger.info(f"Targeting conflicting ISE object by extracted name: '{conflicting_name}'")
                        
                        # FIXED: Fetch the existing record using the internal request wrapper directly
                        lookup_url = f"{self.ise.base_url}/name/{conflicting_name}"
                        res = self.ise._request("GET", lookup_url)
                        conflicting_dev = res.json() if hasattr(res, "json") else res
                        
                        if conflicting_dev and "NetworkDevice" in conflicting_dev:
                            overlapping_id = conflicting_dev["NetworkDevice"]["id"]
                            logger.info(f"Reconciling IP ownership. Updating old device context node ID: {overlapping_id} to new name convention '{sanitized_name}'...")
                            
                            # Bulletproof PUT update
                            update_url = f"{self.ise.base_url}/{overlapping_id}"
                            self.ise._request("PUT", update_url, json=ise_payload)
                            
                            return "Success (Reconciled IP Overlap)"
                        else:
                            raise Exception(f"Conflicting device '{conflicting_name}' could not be fetched from ISE.")
                    else:
                        raise Exception("IP overlap detected, but conflicting device name could not be parsed from the ISE error block.")
                        
                except Exception as lookup_err:
                    logger.error(f"Failed to resolve underlying database IP conflict: {lookup_err}")
                    raise Exception(f"ISE IP Address Overlap Conflict Resolution Failed: {lookup_err}")
            
            else:
                raise Exception(f"Cisco ISE API Rejection: {err_str}")