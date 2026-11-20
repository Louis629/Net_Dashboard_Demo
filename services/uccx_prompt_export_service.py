"""
services/uccx_prompt_export_service.py

Export and Validation Service for Cisco UCCX Custom Audio Prompts.
Recursively traverses UCCX prompt repository trees (including en_US, es_US, and subfolders),
downloads .wav binary streams, verifies WxCC audio compliance (8 kHz, 16-bit/8-bit Mono, < 5 MB),
and stores them under the media repository directory (media/uccx_prompts/).
"""

import logging
import os
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field

from providers.uccx_provider import UCCXProvider, UCCXAPIError

logger = logging.getLogger("UCCX_Prompt_Export_Service")

DEFAULT_PROMPT_EXPORT_DIR = os.getenv("UCCX_PROMPT_EXPORT_DIR", "media/uccx_prompts")


# -----------------------------------------------------------------------------
# Pydantic Output Models (Fully Backward-Compatible)
# -----------------------------------------------------------------------------

class AudioComplianceResult(BaseModel):
    is_compliant: bool
    audio_format: str  # PCM, ULAW, ALAW, UNKNOWN
    channels: int
    sample_rate_hz: int
    bits_per_sample: int
    file_size_bytes: int
    file_size_kb: float
    rejection_reasons: List[str] = Field(default_factory=list)


class PromptExportRecord(BaseModel):
    file_name: str
    folder_path: str
    relative_file_path: str
    file_size_bytes: int
    file_size_kb: float
    wxcc_size_compliant: bool = True  # Retained for backward compatibility
    wxcc_compliant: bool = True       # Evaluates size + audio format
    audio_format: str = "UNKNOWN"
    sample_rate_hz: int = 0
    channels: int = 0
    bits_per_sample: int = 0
    local_file_path: str
    status: str  # SUCCESS, FAILED
    rejection_reasons: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None


class PromptExportSummary(BaseModel):
    total_discovered: int
    total_downloaded: int
    total_failed: int
    total_compliant: int = 0
    total_non_compliant: int = 0
    total_megabytes_downloaded: float
    export_directory: str
    wxcc_non_compliant_count: int = 0  # Retained for backward compatibility
    languages_processed: List[str]
    prompts: List[PromptExportRecord]


# -----------------------------------------------------------------------------
# Service Implementation
# -----------------------------------------------------------------------------

class UCCXPromptExportService:
    def __init__(self, uccx_provider: UCCXProvider):
        self.uccx = uccx_provider

    @staticmethod
    def inspect_wav_compliance(file_bytes: bytes) -> AudioComplianceResult:
        """
        Parses binary RIFF/WAVE header to verify WxCC format compliance:
        - Container: RIFF WAVE
        - Format Tag: 1 (PCM) or 7 (CCITT G.711 u-law)
        - Channels: 1 (Mono)
        - Sample Rate: 8000 Hz
        - File Size: <= 5 MB (5,242,880 bytes)
        """
        file_size = len(file_bytes)
        file_size_kb = round(file_size / 1024.0, 2)
        reasons: List[str] = []

        if file_size > 5242880:
            reasons.append(f"File size ({file_size_kb} KB) exceeds WxCC maximum limit of 5 MB.")

        if file_size < 44:
            return AudioComplianceResult(
                is_compliant=False,
                audio_format="CORRUPT_HEADER",
                channels=0,
                sample_rate_hz=0,
                bits_per_sample=0,
                file_size_bytes=file_size,
                file_size_kb=file_size_kb,
                rejection_reasons=["File is smaller than minimal 44-byte WAV header."]
            )

        # 1. RIFF Header Verification
        riff_header = file_bytes[0:4]
        wave_header = file_bytes[8:12]
        if riff_header != b"RIFF" or wave_header != b"WAVE":
            return AudioComplianceResult(
                is_compliant=False,
                audio_format="INVALID_CONTAINER",
                channels=0,
                sample_rate_hz=0,
                bits_per_sample=0,
                file_size_bytes=file_size,
                file_size_kb=file_size_kb,
                rejection_reasons=["Container header is not valid RIFF/WAVE audio."]
            )

        # 2. Locate 'fmt ' subchunk dynamically
        fmt_index = file_bytes.find(b"fmt ")
        if fmt_index == -1 or len(file_bytes) < fmt_index + 24:
            return AudioComplianceResult(
                is_compliant=False,
                audio_format="MISSING_FMT_CHUNK",
                channels=0,
                sample_rate_hz=0,
                bits_per_sample=0,
                file_size_bytes=file_size,
                file_size_kb=file_size_kb,
                rejection_reasons=["WAV fmt chunk header could not be parsed."]
            )

        # 3. Unpack fmt fields
        try:
            fmt_data = file_bytes[fmt_index + 8 : fmt_index + 24]
            format_tag, channels, sample_rate, avg_bytes_sec, block_align, bits_per_sample = struct.unpack(
                "<HHIIHH", fmt_data[:16]
            )
        except Exception as exc:
            return AudioComplianceResult(
                is_compliant=False,
                audio_format="MALFORMED_FMT_DATA",
                channels=0,
                sample_rate_hz=0,
                bits_per_sample=0,
                file_size_bytes=file_size,
                file_size_kb=file_size_kb,
                rejection_reasons=[f"Error unpacking fmt chunk: {str(exc)}"]
            )

        format_map = {
            1: "PCM",
            6: "ALAW",
            7: "ULAW",
            17: "ADPCM",
            49: "GSM",
            85: "MP3"
        }
        format_name = format_map.get(format_tag, f"UNKNOWN (0x{format_tag:04X})")

        # 4. Evaluate against Webex Contact Center standards
        if format_tag not in {1, 7}:
            reasons.append(f"Audio format '{format_name}' is not supported by WxCC (Must be PCM or CCITT U-law).")

        if channels != 1:
            reasons.append(f"Audio has {channels} channel(s). WxCC requires single-channel Mono audio.")

        if sample_rate != 8000:
            reasons.append(f"Sample rate is {sample_rate} Hz. WxCC requires exactly 8000 Hz (8 kHz).")

        is_compliant = len(reasons) == 0

        return AudioComplianceResult(
            is_compliant=is_compliant,
            audio_format=format_name,
            channels=channels,
            sample_rate_hz=sample_rate,
            bits_per_sample=bits_per_sample,
            file_size_bytes=file_size,
            file_size_kb=file_size_kb,
            rejection_reasons=reasons
        )

    def export_prompts_recursively(
        self,
        target_language_folders: Optional[List[str]] = None,
        output_dir: Optional[str] = None
    ) -> PromptExportSummary:
        """
        Recursively crawls and exports prompts from targeted language directories.
        Defaults to media/uccx_prompts (or UCCX_PROMPT_EXPORT_DIR).
        """
        destination_root = output_dir or DEFAULT_PROMPT_EXPORT_DIR
        if not target_language_folders:
            target_language_folders = ["en_US", "es_US"]

        logger.info("Starting Multi-Language UCCX Prompt Export to '%s' for: %s", destination_root, target_language_folders)

        all_records: List[PromptExportRecord] = []
        total_discovered_count = 0
        downloaded_count = 0
        failed_count = 0
        compliant_count = 0
        non_compliant_count = 0
        total_bytes_accumulated = 0

        for lang_folder in target_language_folders:
            clean_lang_root = lang_folder.strip("/")
            if clean_lang_root.startswith("user/"):
                clean_lang_root = clean_lang_root[5:]

            discovered = self.uccx.discover_prompts_recursively(clean_lang_root)
            total_discovered_count += len(discovered)

            for prompt_info in discovered:
                file_name = prompt_info["file_name"]
                folder_path = prompt_info["folder_path"]
                rel_path = prompt_info["relative_file_path"]

                dest_dir = Path(destination_root) / folder_path
                dest_dir.mkdir(parents=True, exist_ok=True)
                local_dest_path = dest_dir / file_name

                try:
                    logger.info("Downloading prompt [%s]...", rel_path)
                    audio_bytes = self.uccx.download_prompt_bytes(rel_path)
                    compliance: AudioComplianceResult = self.inspect_wav_compliance(audio_bytes)

                    with open(local_dest_path, "wb") as f:
                        f.write(audio_bytes)

                    size_bytes = len(audio_bytes)
                    size_kb = round(size_bytes / 1024.0, 2)
                    total_bytes_accumulated += size_bytes

                    is_wxcc_size_ok = size_bytes <= 5242880

                    if compliance.is_compliant:
                        compliant_count += 1
                    else:
                        non_compliant_count += 1

                    all_records.append(PromptExportRecord(
                        file_name=file_name,
                        folder_path=folder_path,
                        relative_file_path=rel_path,
                        file_size_bytes=size_bytes,
                        file_size_kb=size_kb,
                        wxcc_size_compliant=is_wxcc_size_ok,
                        wxcc_compliant=compliance.is_compliant,
                        audio_format=compliance.audio_format,
                        sample_rate_hz=compliance.sample_rate_hz,
                        channels=compliance.channels,
                        bits_per_sample=compliance.bits_per_sample,
                        local_file_path=str(local_dest_path.resolve()),
                        status="SUCCESS",
                        rejection_reasons=compliance.rejection_reasons
                    ))
                    downloaded_count += 1

                except Exception as exc:
                    logger.error("Failed downloading prompt '%s': %s", rel_path, exc)
                    all_records.append(PromptExportRecord(
                        file_name=file_name,
                        folder_path=folder_path,
                        relative_file_path=rel_path,
                        file_size_bytes=0,
                        file_size_kb=0.0,
                        wxcc_size_compliant=False,
                        wxcc_compliant=False,
                        audio_format="UNKNOWN",
                        sample_rate_hz=0,
                        channels=0,
                        bits_per_sample=0,
                        local_file_path="",
                        status="FAILED",
                        rejection_reasons=[str(exc)],
                        error_message=str(exc)
                    ))
                    failed_count += 1
                    non_compliant_count += 1

        total_mb = round(total_bytes_accumulated / (1024.0 * 1024.0), 2)

        return PromptExportSummary(
            total_discovered=total_discovered_count,
            total_downloaded=downloaded_count,
            total_failed=failed_count,
            total_compliant=compliant_count,
            total_non_compliant=non_compliant_count,
            total_megabytes_downloaded=total_mb,
            export_directory=str(Path(destination_root).resolve()),
            wxcc_non_compliant_count=non_compliant_count,
            languages_processed=target_language_folders,
            prompts=all_records
        )