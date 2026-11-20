"""
providers/cucm_axl_provider.py

High-Performance Cisco CUCM AXL Provider (CUCM 11.5 and 12.5).
Uses executeSQLQuery over SOAP to bypass standard AXL object-getter throttling.
"""

import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple, Union
import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException
import urllib3

logger = logging.getLogger("CUCM_AXL_Provider")

AXL_NS = {
    "soapenv": "http://schemas.xmlsoap.org/soap/envelope/"
}


class AXLProviderError(Exception):
    pass


class AXLRequestError(AXLProviderError):
    pass


class CUCMAXLProvider:
    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        version: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        timeout: Optional[Union[int, float, Tuple[float, float]]] = None,
        connect_timeout: float = 3.0,
        read_timeout: Optional[float] = None
    ):
        self.host = host or os.getenv("CUCM_AXL_HOST", os.getenv("CUCM_PAN_IP", ""))
        self.username = username or os.getenv("CUCM_AXL_USER", os.getenv("CUCM_AXL_USERNAME", ""))
        self.password = password or os.getenv("CUCM_AXL_PASS", os.getenv("CUCM_AXL_PASSWORD", ""))
        self.version = str(version or os.getenv("CUCM_AXL_VERSION", "12.5")).strip()

        if verify_ssl is not None:
            self.verify_ssl = verify_ssl
        else:
            env_verify = os.getenv("CUCM_AXL_VERIFY_SSL", "false").strip().lower()
            self.verify_ssl = env_verify in {"1", "true", "yes", "y"}

        # Normalize timeout properties while preserving scalar self.timeout for compatibility
        self.connect_timeout = float(os.getenv("CUCM_AXL_CONNECT_TIMEOUT_SECONDS", str(connect_timeout)))
        if timeout is not None:
            if isinstance(timeout, tuple):
                self.connect_timeout = float(timeout[0])
                self.timeout = timeout[1]
            else:
                self.timeout = timeout
        else:
            self.timeout = read_timeout if read_timeout is not None else int(os.getenv("CUCM_AXL_TIMEOUT_SECONDS", "30"))

        self.axl_url = f"https://{self.host}:8443/axl/"

        if not self.host or not self.username or not self.password:
            logger.warning("CUCM AXL Provider initialized with missing credentials or host.")

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.verify = self.verify_ssl

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self.session.headers.update({
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"CUCM:DB ver={self.version} executeSQLQuery"'
        })

    @property
    def request_timeout(self) -> Tuple[float, float]:
        """Returns dual-timeout tuple (connect_timeout, read_timeout) for requests.Session calls."""
        return (self.connect_timeout, float(self.timeout))

    def _build_soap_envelope(self, action_or_sql: str, body_content: Optional[str] = None) -> str:
        """
        Builds a SOAP envelope for AXL.
        Backward-compatible with _build_soap_envelope(sql_query).
        """
        if body_content is None:
            # Single argument passed: legacy executeSQLQuery usage
            action = "executeSQLQuery"
            sanitized_sql = (
                action_or_sql.replace("&", "&amp;")
                             .replace("<", "&lt;")
                             .replace(">", "&gt;")
            )
            content = f"         <sql>{sanitized_sql}</sql>"
        else:
            action = action_or_sql
            content = body_content

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns="http://www.cisco.com/AXL/API/{self.version}">
   <soapenv:Header/>
   <soapenv:Body>
      <ns:{action}>
{content}
      </ns:{action}>
   </soapenv:Body>
</soapenv:Envelope>"""

    def execute_axl_soap(self, action: str, body_xml: str) -> str:
        """Executes an arbitrary SOAP action against the Cisco CUCM AXL endpoint."""
        if not self.host:
            raise AXLProviderError("CUCM host is not configured.")

        payload = self._build_soap_envelope(action, body_xml)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"CUCM:DB ver={self.version} {action}"',
        }

        try:
            response = self.session.post(self.axl_url, data=payload, headers=headers, timeout=self.request_timeout)

            # Check for SOAP fault in response
            try:
                root = ET.fromstring(response.text)
                fault_node = root.find(".//soapenv:Fault", AXL_NS)
                if fault_node is None:
                    fault_node = root.find(".//Fault")

                if fault_node is not None:
                    fault_code = fault_node.findtext("faultcode") or fault_node.findtext(".//faultcode") or ""
                    fault_string = fault_node.findtext("faultstring") or fault_node.findtext(".//faultstring") or ""
                    err_msg = f"{fault_code}: {fault_string}".strip(": ")
                    logger.error("AXL SOAP Fault on %s: %s", action, err_msg)
                    raise AXLRequestError(f"SOAP Fault on {action}: {err_msg}")
            except ET.ParseError:
                pass

            if response.status_code != 200:
                logger.error("AXL %s failed with HTTP %s: %s", action, response.status_code, response.text[:200])
                raise AXLRequestError(f"HTTP {response.status_code}: {response.text}")

            return response.text

        except RequestException as exc:
            logger.exception("Network transport error during AXL %s execution: %s", action, exc)
            raise AXLRequestError(f"AXL Transport Error: {str(exc)}") from exc

    def execute_sql_query(self, sql_query: str) -> List[Dict[str, str]]:
        if not self.host:
            raise AXLProviderError("CUCM host is not configured.")

        sanitized_sql = (
            sql_query.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;")
        )
        response_text = self.execute_axl_soap("executeSQLQuery", f"         <sql>{sanitized_sql}</sql>")
        return self._parse_sql_response(response_text)

    def execute_sql_update(self, sql_update: str) -> Dict[str, Any]:
        """Executes an INSERT, UPDATE, or DELETE query in the CUCM Informix database."""
        sanitized_sql = (
            sql_update.replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;")
        )
        response_text = self.execute_axl_soap("executeSQLUpdate", f"         <sql>{sanitized_sql}</sql>")
        try:
            root = ET.fromstring(response_text)
            rows_updated_node = root.find(".//rowsUpdated")
            rows_count = int(rows_updated_node.text) if rows_updated_node is not None and rows_updated_node.text else 0
            return {"status": "SUCCESS", "rows_updated": rows_count}
        except Exception:
            return {"status": "SUCCESS", "rows_updated": 1}

    def _parse_sql_response(self, xml_response: str) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        try:
            root = ET.fromstring(xml_response)
            
            fault_node = root.find(".//soapenv:Fault", AXL_NS)
            if fault_node is None:
                fault_node = root.find(".//Fault")

            if fault_node is not None:
                fault_str = ET.tostring(fault_node, encoding="unicode")
                logger.error("AXL SOAP Fault detected: %s", fault_str)
                raise AXLRequestError(f"SOAP Fault: {fault_str}")

            row_elements = root.findall(".//row")
            for row_elem in row_elements:
                row_data: Dict[str, str] = {}
                for child in row_elem:
                    tag_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    row_data[tag_name] = child.text if child.text is not None else ""
                rows.append(row_data)

            return rows

        except ET.ParseError as exc:
            logger.error("Failed to parse AXL XML response: %s", exc)
            raise AXLRequestError("Invalid XML response from CUCM AXL") from exc

    def fetch_paginated_sql(self, base_sql: str, chunk_size: int = 1000) -> List[Dict[str, str]]:
        """
        Handles Informix SQL pagination using SKIP / FIRST clauses.
        Safely strips SQL to ensure Regex match and implements a hard circuit-breaker.
        """
        all_results: List[Dict[str, str]] = []
        skip = 0
        max_rows_safety = 100000
        
        clean_sql = base_sql.strip()

        while skip < max_rows_safety:
            paginated_sql = re.sub(
                r"^SELECT\s+",
                f"SELECT SKIP {skip} FIRST {chunk_size} ",
                clean_sql,
                flags=re.IGNORECASE
            )

            logger.info("Executing AXL Paginated Query: SKIP %d, LIMIT %d", skip, chunk_size)
            chunk = self.execute_sql_query(paginated_sql)

            if not chunk:
                break

            all_results.extend(chunk)
            
            if len(chunk) < chunk_size:
                logger.debug("Partial page returned (%d < %d). Pagination complete.", len(chunk), chunk_size)
                break

            skip += chunk_size

        return all_results

    def add_translation_pattern(
        self,
        pattern: str,
        partition: str,
        calling_search_space: str,
        description: Optional[str] = None,
        urgent_priority: bool = True,
    ) -> Dict[str, Any]:
        """
        Creates a Translation Pattern in CUCM to redirect an on-prem extension to Webex Calling / LGW.
        Calls AXL SOAP addTransPattern.
        """
        desc = description or f"Migrated to Webex Calling - Ext {pattern}"
        urgency = "true" if urgent_priority else "false"
        body = f"""         <transPattern>
            <pattern>{pattern}</pattern>
            <description>{desc}</description>
            <usage>Translation</usage>
            <routePartitionName>{partition}</routePartitionName>
            <callingSearchSpaceName>{calling_search_space}</callingSearchSpaceName>
            <patternUrgency>{urgency}</patternUrgency>
            <provideOutsideDialtone>false</provideOutsideDialtone>
         </transPattern>"""

        logger.info("Adding Translation Pattern '%s' in Partition '%s' pointing to CSS '%s'...", pattern, partition, calling_search_space)
        response_text = self.execute_axl_soap("addTransPattern", body)
        uuid = ""
        try:
            root = ET.fromstring(response_text)
            ret = root.find(".//return")
            if ret is not None and ret.text:
                uuid = ret.text.strip("{}")
        except Exception:
            pass

        return {
            "status": "SUCCESS",
            "uuid": uuid,
            "pattern": pattern,
            "partition": partition,
            "calling_search_space": calling_search_space,
        }

    def get_translation_pattern(self, pattern: str, partition: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Queries whether a Translation Pattern exists in numplan."""
        clean_pat = pattern.strip()
        sql = f"""SELECT p.pkid, p.dnorpattern, rp.name as partition_name, css.name as css_name, p.description
FROM numplan p
LEFT JOIN routepartition rp ON p.fkroutepartition = rp.pkid
LEFT JOIN callingsearchspace css ON p.fkcallingsearchspace_translation = css.pkid
WHERE p.dnorpattern = '{clean_pat}' AND p.tkpatternusage = 3"""
        if partition:
            clean_part = partition.strip()
            sql += f" AND rp.name = '{clean_part}'"

        rows = self.execute_sql_query(sql)
        if rows:
            return rows[0]
        return None

    def remove_translation_pattern(self, pattern: str, partition: Optional[str] = None) -> bool:
        """Removes a Translation Pattern from CUCM via removeTransPattern."""
        body = f"""         <pattern>{pattern}</pattern>"""
        if partition:
            body += f"\n         <routePartitionName>{partition}</routePartitionName>"
        logger.info("Removing Translation Pattern '%s' (Partition: %s)...", pattern, partition)
        self.execute_axl_soap("removeTransPattern", body)
        return True

    def get_route_partitions(self) -> List[Dict[str, str]]:
        """
        Retrieves all configured Route Partitions from CUCM via SQL query.
        Returns a list of dicts: [{"pkid": "...", "name": "...", "description": "..."}].
        """
        sql = "SELECT pkid, name, description FROM routepartition WHERE name IS NOT NULL ORDER BY name"
        rows = self.execute_sql_query(sql)
        cleaned: List[Dict[str, str]] = []
        for r in rows:
            name = (r.get("name") or "").strip()
            if name:
                cleaned.append({
                    "pkid": (r.get("pkid") or "").strip(),
                    "name": name,
                    "description": (r.get("description") or "").strip(),
                })
        return cleaned

    def get_gateways_and_route_lists(self) -> List[Dict[str, Any]]:
        """
        Retrieves configured Gateways, SIP Trunks, and Route Lists from CUCM via SQL query.
        Returns a list of dicts: [{"pkid": "...", "name": "...", "description": "...", "class_name": "...", "is_route_list": bool}].
        """
        results: List[Dict[str, Any]] = []
        seen = set()

        # 1. Primary query: query device table joined with typeclass on d.tkclass = tc.enum
        try:
            sql_dev = """SELECT d.pkid, d.name, d.description, tc.name as class_name, d.tkclass
FROM device d
LEFT JOIN typeclass tc ON d.tkclass = tc.enum
WHERE LOWER(tc.name) LIKE '%route%list%'
   OR LOWER(tc.name) LIKE '%gateway%'
   OR LOWER(tc.name) LIKE '%trunk%'
   OR d.tkclass IN (2, 7, 18, 45)
ORDER BY d.name"""
            rows = self.execute_sql_query(sql_dev)
            for r in rows:
                name = (r.get("name") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                tkclass = str(r.get("tkclass", "")).strip()
                raw_class = (r.get("class_name") or "").strip()
                is_rl = "route list" in raw_class.lower() or tkclass in ("7", "45")
                class_label = "Route List" if is_rl else ("Trunk" if ("trunk" in raw_class.lower() or tkclass == "18") else "Gateway")
                results.append({
                    "pkid": (r.get("pkid") or "").strip(),
                    "name": name,
                    "description": (r.get("description") or "").strip(),
                    "class_name": class_label,
                    "is_route_list": is_rl,
                })
            logger.info("Found %d gateways/trunks/route lists via device+typeclass query", len(results))
        except Exception as exc:
            logger.warning("Error querying device table with typeclass for gateways/route lists: %s", exc)

        # 2. Supplementary query: check routelist table joined to device (rl.fkdevice = d.pkid) for any Route Lists
        try:
            sql_rl = """SELECT DISTINCT d.pkid, d.name, d.description, d.tkclass
FROM device d
JOIN routelist rl ON rl.fkdevice = d.pkid
ORDER BY d.name"""
            rl_rows = self.execute_sql_query(sql_rl)
            for r in rl_rows:
                name = (r.get("name") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    results.append({
                        "pkid": (r.get("pkid") or "").strip(),
                        "name": name,
                        "description": (r.get("description") or "").strip(),
                        "class_name": "Route List",
                        "is_route_list": True,
                    })
        except Exception as exc:
            logger.debug("Error querying routelist join device: %s", exc)

        # 3. Supplementary query: check sipdevice table joined to device (sd.fkdevice = d.pkid) for any SIP Trunks
        try:
            sql_sip = """SELECT DISTINCT d.pkid, d.name, d.description, d.tkclass
FROM device d
JOIN sipdevice sd ON sd.fkdevice = d.pkid
ORDER BY d.name"""
            sip_rows = self.execute_sql_query(sql_sip)
            for r in sip_rows:
                name = (r.get("name") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    results.append({
                        "pkid": (r.get("pkid") or "").strip(),
                        "name": name,
                        "description": (r.get("description") or "").strip(),
                        "class_name": "Trunk",
                        "is_route_list": False,
                    })
        except Exception as exc:
            logger.debug("Error querying sipdevice join device: %s", exc)

        results.sort(key=lambda x: x["name"].upper())
        return results

    def add_route_pattern(
        self,
        pattern: str,
        partition: str,
        gateway_route_list: str,
        is_route_list: Optional[bool] = None,
        description: Optional[str] = None,
        urgent_priority: bool = True,
    ) -> Dict[str, Any]:
        """
        Creates a Route Pattern in CUCM to redirect an on-prem extension directly to a Gateway or Route List.
        Calls AXL SOAP addRoutePattern.
        """
        desc = description or f"Migrated to Webex Calling - Ext {pattern}"
        urgency = "true" if urgent_priority else "false"

        # Auto-determine if gateway_route_list is a Route List or Gateway if not provided
        if is_route_list is None:
            try:
                check_sql = f"""SELECT d.tkclass, tc.name as class_name
FROM device d
LEFT JOIN typeclass tc ON d.tkclass = tc.enum
WHERE d.name = '{gateway_route_list}'"""
                rows = self.execute_sql_query(check_sql)
                if rows:
                    raw_cls = (rows[0].get("class_name") or "").lower()
                    tkcls = str(rows[0].get("tkclass", "")).strip()
                    if "route list" in raw_cls or tkcls in ("7", "45"):
                        is_route_list = True
                    else:
                        is_route_list = False
                else:
                    rl_check = self.execute_sql_query(f"""SELECT d.pkid
FROM device d
JOIN routelist rl ON rl.fkdevice = d.pkid
WHERE d.name = '{gateway_route_list}'""")
                    is_route_list = bool(rl_check)
            except Exception:
                is_route_list = False

        dest_tag = "routeListName" if is_route_list else "gatewayName"

        body = f"""         <routePattern>
            <pattern>{pattern}</pattern>
            <description>{desc}</description>
            <routePartitionName>{partition}</routePartitionName>
            <destination>
                <{dest_tag}>{gateway_route_list}</{dest_tag}>
            </destination>
            <patternUrgency>{urgency}</patternUrgency>
            <provideOutsideDialtone>false</provideOutsideDialtone>
         </routePattern>"""

        logger.info("Adding Route Pattern '%s' in Partition '%s' pointing to %s '%s'...",
                    pattern, partition, dest_tag, gateway_route_list)
        response_text = self.execute_axl_soap("addRoutePattern", body)
        uuid = ""
        try:
            root = ET.fromstring(response_text)
            ret = root.find(".//return")
            if ret is not None and ret.text:
                uuid = ret.text.strip("{}")
        except Exception:
            pass

        return {
            "status": "SUCCESS",
            "uuid": uuid,
            "pattern": pattern,
            "partition": partition,
            "gateway_route_list": gateway_route_list,
            "destination_type": "Route List" if is_route_list else "Gateway/Trunk",
        }

    def get_route_pattern(self, pattern: str, partition: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Queries whether a Route Pattern exists in numplan."""
        clean_pat = pattern.strip()
        sql = f"""SELECT p.pkid, p.dnorpattern as pattern, rp.name as partition_name, d.name as destination_name, tc.name as destination_type, p.description
FROM numplan p
LEFT JOIN routepartition rp ON p.fkroutepartition = rp.pkid
LEFT JOIN devicenumplanmap dnm ON dnm.fknumplan = p.pkid
LEFT JOIN device d ON dnm.fkdevice = d.pkid
LEFT JOIN typeclass tc ON d.tkclass = tc.enum
WHERE p.dnorpattern = '{clean_pat}' AND p.tkpatternusage = 5"""
        if partition:
            clean_part = partition.strip()
            sql += f" AND rp.name = '{clean_part}'"

        rows = self.execute_sql_query(sql)
        if rows:
            return rows[0]
        return None

    def remove_route_pattern(self, pattern: str, partition: Optional[str] = None) -> bool:
        """Removes a Route Pattern from CUCM via removeRoutePattern."""
        body = f"""         <pattern>{pattern}</pattern>"""
        if partition:
            body += f"\n         <routePartitionName>{partition}</routePartitionName>"
        logger.info("Removing Route Pattern '%s' (Partition: %s)...", pattern, partition)
        self.execute_axl_soap("removeRoutePattern", body)
        return True

    def get_device_by_name_or_mac(self, device_name_or_mac: str) -> Optional[Dict[str, Any]]:
        """Queries CUCM device and associated lines for a MAC address or CUCM device name."""
        clean = device_name_or_mac.strip().upper()
        if not clean.startswith(("CSF", "TCT", "BOT", "TAB")):
            raw_mac = clean[3:] if clean.startswith("SEP") else clean
            hex_mac = raw_mac.replace(":", "").replace("-", "").replace(".", "")
            if len(hex_mac) == 12:
                clean = f"SEP{hex_mac}"

        sql = f"""SELECT d.pkid, d.name as device_name, tm.name as model, d.description, dp.name as device_pool,
       u.userid as owner_userid
FROM device d
LEFT JOIN typemodel tm ON d.tkmodel = tm.enum
LEFT JOIN devicepool dp ON d.fkdevicepool = dp.pkid
LEFT JOIN enduser u ON d.fkenduser = u.pkid
WHERE UPPER(d.name) = '{clean}' OR d.name = '{clean}'"""

        dev_rows = self.execute_sql_query(sql)
        if not dev_rows:
            return None

        dev: Dict[str, Any] = dict(dev_rows[0])
        dev["lines"] = []
        pkid = dev.get("pkid") or ""
        if pkid:
            try:
                line_sql = f"""SELECT np.dnorpattern as line_dn, rp.name as partition_name, dnm.numplanindex
FROM devicenumplanmap dnm
JOIN numplan np ON dnm.fknumplan = np.pkid
LEFT JOIN routepartition rp ON np.fkroutepartition = rp.pkid
WHERE dnm.fkdevice = '{pkid}'
ORDER BY dnm.numplanindex"""
                dev["lines"] = self.execute_sql_query(line_sql)
            except Exception as line_err:
                logger.warning("Could not query lines for device '%s': %s", clean, line_err)

        return dev

    def get_devices_by_extension(self, pattern: str, partition: Optional[str] = None) -> List[Dict[str, Any]]:
        """Queries all CUCM devices (physical or softphone/BOT/TCT/CSF) sharing a Directory Number."""
        clean_pat = pattern.strip()
        sql = f"""SELECT d.pkid, d.name as device_name, tm.name as model, d.description, dp.name as device_pool,
       u.userid as owner_userid, np.dnorpattern as line_dn, rp.name as partition_name
FROM device d
JOIN devicenumplanmap dnm ON dnm.fkdevice = d.pkid
JOIN numplan np ON dnm.fknumplan = np.pkid
LEFT JOIN routepartition rp ON np.fkroutepartition = rp.pkid
LEFT JOIN typemodel tm ON d.tkmodel = tm.enum
LEFT JOIN devicepool dp ON d.fkdevicepool = dp.pkid
LEFT JOIN enduser u ON d.fkenduser = u.pkid
WHERE np.dnorpattern = '{clean_pat}' AND np.tkpatternusage = 2"""
        if partition and partition.strip():
            clean_part = partition.strip()
            sql += f"\n  AND rp.name = '{clean_part}'"
        sql += "\nORDER BY d.name"
        return [dict(r) for r in self.execute_sql_query(sql)]

    def remove_phone(self, device_name_or_mac: str) -> bool:
        """Deletes a physical IP phone or softphone device from CUCM via removePhone to release UCL/CUWL Smart Licenses."""
        raw = device_name_or_mac.strip()
        upper = raw.upper()
        if upper.startswith("SEP"):
            hex_part = upper[3:].replace(":", "").replace("-", "").replace(".", "")
            clean = f"SEP{hex_part}" if len(hex_part) == 12 else raw
        else:
            hex_part = upper.replace(":", "").replace("-", "").replace(".", "")
            if len(hex_part) == 12 and all(ch in "0123456789ABCDEF" for ch in hex_part):
                clean = f"SEP{hex_part}"
            elif upper.startswith(("CSF", "TCT", "BOT", "TAB")) and raw.isupper():
                clean = upper
            else:
                # Preserve original mixed case for custom CUCM device names (e.g. ThomasJabber)
                clean = raw

        body = f"         <name>{clean}</name>"
        logger.info("Decommissioning device '%s' from CUCM via removePhone...", clean)
        self.execute_axl_soap("removePhone", body)
        return True

    def remove_line(self, pattern: str, partition: Optional[str] = None) -> bool:
        """Deletes an unassigned directory number pattern from numplan via removeLine."""
        body = f"         <pattern>{pattern}</pattern>"
        if partition:
            body += f"\n         <routePartitionName>{partition}</routePartitionName>"
        logger.info("Removing Directory Number line '%s' (Partition: %s) via removeLine...", pattern, partition)
        self.execute_axl_soap("removeLine", body)
        return True