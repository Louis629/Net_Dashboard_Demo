from security.roles import Role

PERMISSIONS = {
    Role.HELPDESK: [
    "identityTracker",
    "sessionTriage",
    "windowsDiagnostics",
    "networkUtility",
    "iperf"
	],

    Role.LOCAL_TECH_SERVICES: [
        "identityTracker",
		"sessionTriage",
		"windowsDiagnostics",
		"networkUtility",
		"iperf",
        "imagingBypassExecute",
		"sccmDeleteDevice"
    ],

    Role.NETWORK_ENGINEER: [
        "identityTracker",
		"sessionTriage",
		"windowsDiagnostics",
		"networkUtility",
		"iperf",
        "imagingBypassExecute",
        "ndgAudit"
    ],

    Role.NETWORK_ADMIN: [
        "identityTracker",
		"sessionTriage",
		"windowsDiagnostics",
		"networkUtility",
		"iperf",
        "imagingBypassExecute",
        "ndgAudit",
        "ndgSync",
		"sccmDeleteDevice"
    ]
}