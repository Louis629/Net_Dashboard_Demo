"""
tests/test_webex_calling_admin_roles_preservation.py

Unit test suite to verify that WebexCallingProvider preserves user administrative roles,
siteAdmin flags, orgId, and attributes during PUT /people/{personId} updates:
- enable_webex_calling_for_person
- move_person_location
"""

from unittest.mock import MagicMock, patch
import pytest

from providers.webex_calling_provider import WebexCallingProvider


def test_enable_webex_calling_preserves_admin_roles_and_attributes():
    provider = WebexCallingProvider(access_token="test_token")
    admin_user = {
        "id": "person_admin_123",
        "firstName": "John",
        "lastName": "Admin",
        "displayName": "John Admin",
        "emails": ["jadmin@corp.internal"],
        "licenses": ["lic_meetings_123"],
        "locationId": "loc_initial",
        "roles": [
            "Y2lzY29zcGFyazovL3VzL1JPTEUvX29yZ19hZG1pbg",
            "Y2lzY29zcGFyazovL3VzL1JPTEUvY2FsbGluZ19hZG1pbg",
        ],
        "siteAdmin": True,
        "orgId": "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9zb25pYw",
        "loginEnabled": True,
        "title": "Lead Telecommunications Engineer",
        "department": "Infrastructure Services",
    }

    with patch.object(provider, "_request") as mock_req:
        def fake_request(method, endpoint, **kwargs):
            if method == "GET" and "/people/person_admin_123" in endpoint:
                return admin_user
            if method == "PUT" and "/people/person_admin_123" in endpoint:
                return {"id": "person_admin_123", **kwargs.get("json", {})}
            return {}

        mock_req.side_effect = fake_request

        res = provider.enable_webex_calling_for_person(
            person_id="person_admin_123",
            location_id="loc_initial",
            calling_license_id="lic_calling_prof_456",
            extension="4455",
            phone_number="+18085550199",
        )

        assert res["id"] == "person_admin_123"

        # Find the PUT /people/person_admin_123 call
        put_calls = [c for c in mock_req.call_args_list if c[0][0] == "PUT" and "/people/person_admin_123" in c[0][1]]
        assert len(put_calls) == 1
        put_payload = put_calls[0][1]["json"]

        # CRITICAL: Verify roles were NOT stripped
        assert put_payload["roles"] == [
            "Y2lzY29zcGFyazovL3VzL1JPTEUvX29yZ19hZG1pbg",
            "Y2lzY29zcGFyazovL3VzL1JPTEUvY2FsbGluZ19hZG1pbg",
        ]
        assert put_payload["siteAdmin"] is True
        assert put_payload["orgId"] == "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9zb25pYw"
        assert put_payload["loginEnabled"] is True
        assert put_payload["title"] == "Lead Telecommunications Engineer"
        assert put_payload["department"] == "Infrastructure Services"
        assert "lic_calling_prof_456" in put_payload["licenses"]
        assert put_payload["extension"] == "4455"
        assert put_payload["phoneNumbers"] == [{"type": "work", "value": "+18085550199"}]


def test_move_person_location_preserves_admin_roles_and_attributes():
    provider = WebexCallingProvider(access_token="test_token")
    admin_user = {
        "id": "person_admin_123",
        "firstName": "John",
        "lastName": "Admin",
        "displayName": "John Admin",
        "emails": ["jadmin@corp.internal"],
        "licenses": ["lic_calling_prof_456"],
        "locationId": "loc_old",
        "roles": [
            "Y2lzY29zcGFyazovL3VzL1JPTEUvX29yZ19hZG1pbg",
        ],
        "siteAdmin": False,
        "orgId": "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9zb25pYw",
        "loginEnabled": True,
        "title": "Director of Voice Services",
    }

    with patch.object(provider, "get_person_by_id", return_value=admin_user), \
         patch.object(provider, "_request") as mock_req:

        # Let move actions fail to trigger Attempt 3 (direct PUT /people/{personId})
        def fake_request(method, endpoint, **kwargs):
            if "actions/moveLocation" in endpoint or "jobs/person/moveLocation" in endpoint:
                raise Exception("Endpoint not supported")
            if method == "PUT" and "/people/person_admin_123" in endpoint:
                return {"id": "person_admin_123", **kwargs.get("json", {})}
            return {}

        mock_req.side_effect = fake_request

        res = provider.move_person_location(
            person_id="person_admin_123",
            location_id="loc_new",
            phone_number="+18085550199",
            extension="4455",
        )

        assert res["id"] == "person_admin_123"

        put_calls = [c for c in mock_req.call_args_list if c[0][0] == "PUT" and "/people/person_admin_123" in c[0][1]]
        assert len(put_calls) == 1
        put_payload = put_calls[0][1]["json"]

        # Verify roles, siteAdmin, orgId, and attributes are intact
        assert put_payload["roles"] == ["Y2lzY29zcGFyazovL3VzL1JPTEUvX29yZ19hZG1pbg"]
        assert put_payload["siteAdmin"] is False
        assert put_payload["orgId"] == "Y2lzY29zcGFyazovL3VzL09SR0FOSVpBVElPTi9zb25pYw"
        assert put_payload["loginEnabled"] is True
        assert put_payload["title"] == "Director of Voice Services"
        assert put_payload["locationId"] == "loc_new"
