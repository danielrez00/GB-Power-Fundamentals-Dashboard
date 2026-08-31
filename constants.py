GEN_CODES = ["T", "V", "E"]  # bm unit types that generate

INTERCONNECTOR_CODE = "I"  # bm unit type for interconnectors


INTERCONNECTOR_ADMIN_PARTIES = {"NESO"}  # lead party on the admin pairs
ADMIN_FUEL = "INTERCONNECTOR_ADMIN"  # tag used to hold the admin pairs out

EXCLUDE_INTERCONNECTOR_OWNERS = False  # switch to drop cable owner units too

STACK_CODES = GEN_CODES + [INTERCONNECTOR_CODE]  # unit types in the declared stack

ADMIN_CABLES = {  # id suffix to cable name for owner and admin units
    "BRTN1": "BritNed",
    "ELEC1": "ElecLink",
    "EWIC1": "EWIC",
    "FRAN1": "IFA",
    "GRN1": "Greenlink",
    "IFA2": "IFA2",
    "MOYL1": "Moyle",
    "NEMO1": "Nemo",
    "NSL1": "NSL",
    "VKL1": "Viking",
}

# days cached by data_pull and listed in the sidebar
DEMO_DATES = ["2026-04-24", "2026-03-25", "2026-01-06", "2026-05-31"]

SHALLOW_MW = 500  # shallow depth for the turn up panel in mw
DEEP_MW = 1500  # deep depth for the turn up panel in mw

HISTORY_DAYS = 30  # length of the reference band window in days

# these days do not have 48 settlement periods
CLOCK_CHANGE = {"2026-03-29", "2026-10-25", "2025-03-30", "2025-10-26"}

FUEL_ORDER = [  # stacking order used by every chart
    "NUCLEAR",
    "WIND",
    "NPSHYD",
    "BIOMASS",
    "CCGT",
    "COAL",
    "OCGT",
    "PS",
    "BATTERY",
    "INTERCONNECTOR",
    "OTHER",
    "UNKNOWN",
]

USER_CABLES = {  # trader id prefix to cable resolved by interconnectors py
    "IN": "Nemo",
    "I2": "IFA2",
    "IL": "ElecLink",
    "IS": "NSL",
    "IF": "IFA",
    "IV": "Viking",
    "IB": "BritNed",
}

FUELINST_CABLES = {  # fuelinst fuel code to cable name
    "INTFR": "IFA",
    "INTIFA2": "IFA2",
    "INTNED": "BritNed",
    "INTNEM": "Nemo",
    "INTELEC": "ElecLink",
    "INTNSL": "NSL",
    "INTVKL": "Viking",
    "INTEW": "EWIC",
    "INTIRL": "Moyle",
    "INTGRNL": "Greenlink",
}
