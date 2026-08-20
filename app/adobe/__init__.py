"""Adobe automation layer: detection, scripting bridge and controllers."""

from app.adobe.bridge import AdobeBridge, ScriptResult
from app.adobe.detect import AdobeApp, detect_all, detect_indesign, detect_photoshop
from app.adobe.indesign.controller import InDesignController
from app.adobe.jsx import Script, ScriptBuilder, build_document_script, build_page_script
from app.adobe.photoshop.controller import PhotoshopController
from app.adobe.service import AdobeService, HealthCheck, HealthReport

__all__ = [
    "AdobeApp",
    "detect_indesign",
    "detect_photoshop",
    "detect_all",
    "AdobeBridge",
    "ScriptResult",
    "Script",
    "ScriptBuilder",
    "build_page_script",
    "build_document_script",
    "InDesignController",
    "PhotoshopController",
    "AdobeService",
    "HealthReport",
    "HealthCheck",
]
