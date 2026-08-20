"""AI Settings, Adobe Settings and System Diagnostics."""

from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
)

from app.config.secrets import mask
from app.ui.pages.base import Page
from app.ui.widgets.common import DataTable, Toolbar, run_guarded

log = logging.getLogger(__name__)

#: The InDesign document a run builds into, in the order the settings offer it.
DOCUMENT_SOURCES = (
    ("auto", "The document already open, then the template's file, then a new one"),
    ("open_document", "Only the document already open in InDesign"),
    ("template_file", "Only the InDesign file the template names"),
    ("new_document", "Always a new, blank document"),
)

PROVIDERS = ["heuristic", "openai", "anthropic", "gemini", "local"]
IMAGE_PROVIDERS = ["none", "openai", "gemini", "stablediffusion", "local"]


class AISettingsPage(Page):
    """Choose the provider, the models and store the API keys securely."""

    title = "AI Settings"
    subtitle = "Providers are interchangeable. Keys are stored in the operating system's credential store, never in a file."
    icon = "✦"

    def build(self) -> None:
        """Create the provider forms."""
        text_group = QGroupBox("Text and vision")
        text_form = QFormLayout(text_group)
        self.provider_box = QComboBox()
        self.provider_box.addItems(PROVIDERS)
        self.model_edit = QLineEdit()
        self.vision_provider_box = QComboBox()
        self.vision_provider_box.addItems(PROVIDERS)
        self.vision_model_edit = QLineEdit()
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("Leave empty for the provider's default endpoint")
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(256, 200_000)
        self.max_tokens_spin.setSingleStep(256)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(5.0, 3600.0)
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 10)
        text_form.addRow("Text provider", self.provider_box)
        text_form.addRow("Text model", self.model_edit)
        text_form.addRow("Vision provider", self.vision_provider_box)
        text_form.addRow("Vision model", self.vision_model_edit)
        text_form.addRow("Endpoint override", self.base_url_edit)
        text_form.addRow("Temperature", self.temperature_spin)
        text_form.addRow("Max tokens", self.max_tokens_spin)
        text_form.addRow("Timeout (s)", self.timeout_spin)
        text_form.addRow("Retries", self.retries_spin)
        self.root.addWidget(text_group)

        image_group = QGroupBox("Image generation")
        image_form = QFormLayout(image_group)
        self.image_provider_box = QComboBox()
        self.image_provider_box.addItems(IMAGE_PROVIDERS)
        self.image_model_edit = QLineEdit()
        self.image_style_edit = QLineEdit()
        self.image_negative_edit = QLineEdit()
        self.image_base_url_edit = QLineEdit()
        self.image_base_url_edit.setPlaceholderText(
            "http://127.0.0.1:7860 for a local Stable Diffusion server"
        )
        image_form.addRow("Provider", self.image_provider_box)
        image_form.addRow("Model", self.image_model_edit)
        image_form.addRow("Default style", self.image_style_edit)
        image_form.addRow("Negative prompt", self.image_negative_edit)
        image_form.addRow("Endpoint override", self.image_base_url_edit)
        self.root.addWidget(image_group)

        keys_group = QGroupBox("API keys")
        keys_form = QFormLayout(keys_group)
        self.key_provider_box = QComboBox()
        self.key_provider_box.addItems(["openai", "anthropic", "gemini", "stablediffusion", "local"])
        self.key_provider_box.currentTextChanged.connect(self._show_key_state)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste the key; it is written to the credential store")
        self.key_state = QLabel("")
        self.key_state.setObjectName("Subtitle")
        keys_form.addRow("Provider", self.key_provider_box)
        keys_form.addRow("Key", self.key_edit)
        keys_form.addRow("", self.key_state)
        self.root.addWidget(keys_group)

        toolbar = Toolbar()
        self.test_button = toolbar.add(QPushButton("Test connection"))
        self.test_button.clicked.connect(self._test)
        toolbar.stretch()
        self.save_key_button = toolbar.add(QPushButton("Save key"))
        self.save_key_button.clicked.connect(self._save_key)
        self.clear_key_button = toolbar.add(QPushButton("Remove key"))
        self.clear_key_button.clicked.connect(self._clear_key)
        self.save_button = toolbar.add(QPushButton("Save settings"))
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        self.root.addWidget(toolbar)

        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMaximumHeight(150)
        self.root.addWidget(self.result)
        self.root.addStretch(1)

    def refresh(self) -> None:
        """Load the current settings into the form."""
        ai = self.app.settings.settings.ai
        image = self.app.settings.settings.image_ai
        self.provider_box.setCurrentText(ai.provider)
        self.model_edit.setText(ai.model)
        self.vision_provider_box.setCurrentText(ai.vision_provider)
        self.vision_model_edit.setText(ai.vision_model)
        self.base_url_edit.setText(ai.base_url or "")
        self.temperature_spin.setValue(ai.temperature)
        self.max_tokens_spin.setValue(ai.max_tokens)
        self.timeout_spin.setValue(ai.timeout_seconds)
        self.retries_spin.setValue(ai.max_retries)
        self.image_provider_box.setCurrentText(image.provider)
        self.image_model_edit.setText(image.model)
        self.image_style_edit.setText(image.default_style)
        self.image_negative_edit.setText(image.negative_prompt)
        self.image_base_url_edit.setText(image.base_url or "")
        self._show_key_state(self.key_provider_box.currentText())

    def _show_key_state(self, provider: str) -> None:
        value = self.app.settings.api_key(provider)
        backend = self.app.settings.secrets.backend_name()
        self.key_state.setText(f"Stored: {mask(value)}  -  backend: {backend}")

    def _save(self) -> None:
        def action() -> None:
            self.app.settings.update(
                ai={
                    "provider": self.provider_box.currentText(),
                    "model": self.model_edit.text().strip() or "gpt-4o-mini",
                    "vision_provider": self.vision_provider_box.currentText(),
                    "vision_model": self.vision_model_edit.text().strip() or "gpt-4o",
                    "base_url": self.base_url_edit.text().strip() or None,
                    "temperature": self.temperature_spin.value(),
                    "max_tokens": self.max_tokens_spin.value(),
                    "timeout_seconds": self.timeout_spin.value(),
                    "max_retries": self.retries_spin.value(),
                },
                image_ai={
                    "provider": self.image_provider_box.currentText(),
                    "model": self.image_model_edit.text().strip() or "gpt-image-1",
                    "default_style": self.image_style_edit.text().strip(),
                    "negative_prompt": self.image_negative_edit.text().strip(),
                    "base_url": self.image_base_url_edit.text().strip() or None,
                },
            )
            self.app.reload_ai()
            self.result.setPlainText("Settings saved and the providers were rebuilt.")

        run_guarded(self, "Save AI settings", action)

    def _save_key(self) -> None:
        provider = self.key_provider_box.currentText()
        value = self.key_edit.text().strip()
        if not value:
            return

        def action() -> None:
            self.app.settings.set_api_key(provider, value)
            self.key_edit.clear()
            self._show_key_state(provider)
            self.app.reload_ai()
            self.result.setPlainText(f"Key for '{provider}' stored in the credential store.")

        run_guarded(self, "Save API key", action)

    def _clear_key(self) -> None:
        provider = self.key_provider_box.currentText()

        def action() -> None:
            self.app.settings.set_api_key(provider, "")
            self._show_key_state(provider)
            self.app.reload_ai()
            self.result.setPlainText(f"Key for '{provider}' removed.")

        run_guarded(self, "Remove API key", action)

    def _test(self) -> None:
        def action() -> None:
            lines = [
                f"{h.name}: {'ok' if h.available else 'unavailable'} - {h.detail}"
                for h in self.app.ai.health()
            ]
            self.result.setPlainText("\n".join(lines))

        run_guarded(self, "Test AI connection", action)


class AdobeSettingsPage(Page):
    """Adobe discovery, automation preferences and the pre-flight check."""

    title = "Adobe Settings"
    subtitle = "Scripting first, UI automation only as a fallback, synthetic input only if you allow it."
    icon = "◈"

    def build(self) -> None:
        """Create the path form and the automation preferences."""
        paths_group = QGroupBox("Applications")
        form = QFormLayout(paths_group)
        self.indesign_edit = QLineEdit()
        self.indesign_button = QPushButton("Browse…")
        self.indesign_button.clicked.connect(lambda: self._browse(self.indesign_edit, "InDesign"))
        self.photoshop_edit = QLineEdit()
        self.photoshop_button = QPushButton("Browse…")
        self.photoshop_button.clicked.connect(lambda: self._browse(self.photoshop_edit, "Photoshop"))
        form.addRow("InDesign", self.indesign_edit)
        form.addRow("", self.indesign_button)
        form.addRow("Photoshop", self.photoshop_edit)
        form.addRow("", self.photoshop_button)
        self.detected = QLabel("")
        self.detected.setObjectName("Subtitle")
        self.detected.setWordWrap(True)
        form.addRow("Detected", self.detected)
        self.root.addWidget(paths_group)

        automation_group = QGroupBox("Automation")
        automation_form = QFormLayout(automation_group)
        self.prefer_com = QCheckBox("Use the COM scripting API first (recommended)")
        self.allow_ui = QCheckBox("Allow the Windows UI Automation fallback")
        self.allow_input = QCheckBox("Allow mouse and keyboard automation (last resort)")
        self.close_docs = QCheckBox("Close the Adobe documents when a run finishes")
        self.script_timeout = QDoubleSpinBox()
        self.script_timeout.setRange(10.0, 7200.0)
        self.launch_timeout = QDoubleSpinBox()
        self.launch_timeout.setRange(10.0, 1200.0)
        automation_form.addRow("", self.prefer_com)
        automation_form.addRow("", self.allow_ui)
        automation_form.addRow("", self.allow_input)
        automation_form.addRow("", self.close_docs)
        automation_form.addRow("Script timeout (s)", self.script_timeout)
        automation_form.addRow("Launch timeout (s)", self.launch_timeout)
        self.root.addWidget(automation_group)

        document_group = QGroupBox("Where the edition is built")
        document_form = QFormLayout(document_group)
        self.document_source = QComboBox()
        for value, label in DOCUMENT_SOURCES:
            self.document_source.addItem(label, value)
        self.document_source.setToolTip(
            "Which InDesign document the pages are placed into when a run starts."
        )
        self.adopt_geometry = QCheckBox("Lay the edition out to fit the open document's own page setup")
        self.adopt_geometry.setToolTip(
            "A document set up by hand rarely matches the template exactly. With this on, the page "
            "size, margins and columns are read from that document and the layout is planned to fit "
            "it. With it off, a document whose page differs is not used at all."
        )
        document_form.addRow("Document", self.document_source)
        document_form.addRow("", self.adopt_geometry)
        self.root.addWidget(document_group)

        toolbar = Toolbar()
        self.detect_button = toolbar.add(QPushButton("Detect again"))
        self.detect_button.clicked.connect(self._detect)
        self.check_button = toolbar.add(QPushButton("Run the pre-flight check"))
        self.check_button.clicked.connect(self._health)
        toolbar.stretch()
        self.save_button = toolbar.add(QPushButton("Save"))
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        self.root.addWidget(toolbar)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.root.addWidget(self.output, 1)

    def refresh(self) -> None:
        """Load the settings and the detection result."""
        adobe = self.app.settings.settings.adobe
        self.indesign_edit.setText(adobe.indesign_path or "")
        self.photoshop_edit.setText(adobe.photoshop_path or "")
        self.prefer_com.setChecked(adobe.prefer_com)
        self.allow_ui.setChecked(adobe.allow_ui_automation)
        self.allow_input.setChecked(adobe.allow_input_automation)
        self.close_docs.setChecked(adobe.close_documents_on_finish)
        index = self.document_source.findData(adobe.document_source)
        self.document_source.setCurrentIndex(max(0, index))
        self.adopt_geometry.setChecked(adobe.adopt_open_geometry)
        self.script_timeout.setValue(adobe.script_timeout_seconds)
        self.launch_timeout.setValue(adobe.launch_timeout_seconds)
        self.detected.setText(
            f"{self.app.adobe.indesign_app.summary()}\n{self.app.adobe.photoshop_app.summary()}\n"
            f"Strategy chain: {', '.join(self.app.adobe.indesign.bridge.fallback_chain())}"
        )

    def _browse(self, edit: QLineEdit, name: str) -> None:
        path, _f = QFileDialog.getOpenFileName(self, f"Locate {name}", "", "Programs (*.exe);;All files (*)")
        if path:
            edit.setText(path)

    def _save(self) -> None:
        def action() -> None:
            self.app.settings.update(
                adobe={
                    "indesign_path": self.indesign_edit.text().strip() or None,
                    "photoshop_path": self.photoshop_edit.text().strip() or None,
                    "prefer_com": self.prefer_com.isChecked(),
                    "allow_ui_automation": self.allow_ui.isChecked(),
                    "allow_input_automation": self.allow_input.isChecked(),
                    "close_documents_on_finish": self.close_docs.isChecked(),
                    "document_source": self.document_source.currentData(),
                    "adopt_open_geometry": self.adopt_geometry.isChecked(),
                    "script_timeout_seconds": self.script_timeout.value(),
                    "launch_timeout_seconds": self.launch_timeout.value(),
                }
            )
            self.app.reload_adobe()
            self.refresh()
            self.output.setPlainText("Adobe settings saved; detection was re-run.")

        run_guarded(self, "Save Adobe settings", action)

    def _detect(self) -> None:
        def action() -> None:
            self.app.reload_adobe()
            self.app.adobe.persist_detection()
            self.refresh()
            self.output.setPlainText(
                "\n".join(
                    f"{name}: {app.summary()}" + ("\n  " + "\n  ".join(app.notes) if app.notes else "")
                    for name, app in (
                        ("InDesign", self.app.adobe.indesign_app),
                        ("Photoshop", self.app.adobe.photoshop_app),
                    )
                )
            )

        run_guarded(self, "Detect Adobe", action)

    def _health(self) -> None:
        def done(text: str) -> None:
            self.output.setPlainText(text)

        self.run_background(
            "adobe-health",
            "Running the Adobe pre-flight check",
            lambda: self.app.adobe.health_check(deep=True).render(),
            on_success=done,
            busy_widgets=[self.check_button, self.detect_button, self.save_button],
        )


class DiagnosticsPage(Page):
    """System diagnostics (specification §49)."""

    title = "Diagnostics"
    subtitle = "Everything the application depends on, checked in one place."
    icon = "✚"

    def build(self) -> None:
        """Create the check table."""
        toolbar = Toolbar()
        self.run_button = toolbar.add(QPushButton("Run checks"))
        self.run_button.setObjectName("Primary")
        self.run_button.clicked.connect(lambda: self._run(deep=False))
        self.deep_button = toolbar.add(QPushButton("Run deep checks (contacts Adobe and the AI provider)"))
        self.deep_button.clicked.connect(lambda: self._run(deep=True))
        toolbar.stretch()
        self.copy_button = toolbar.add(QPushButton("Copy report"))
        self.copy_button.clicked.connect(self._copy)
        self.root.addWidget(toolbar)

        self.table = DataTable(["Check", "Status", "Detail"])
        self.root.addWidget(self.table, 1)
        self.summary = QLabel("")
        self.summary.setObjectName("Subtitle")
        self.summary.setWordWrap(True)
        self.root.addWidget(self.summary)
        self._report_text = ""

    def refresh(self) -> None:
        """Run the shallow checks when the page is shown."""
        if self.table.rowCount() == 0:
            self._run(deep=False)

    def _run(self, deep: bool):
        """Run the checks off the GUI thread; returns the task for tests."""

        def done(report) -> None:
            self._report_text = report.render()
            self.table.fill(
                [[c.name, c.status, c.detail] for c in report.checks],
                user_data=[c.name for c in report.checks],
            )
            errors = len(report.by_status("error"))
            warnings = len(report.by_status("warning"))
            self.summary.setText(
                f"{'All checks passed.' if report.ok else 'Problems found.'}  "
                f"{errors} error(s), {warnings} warning(s)."
            )

        return self.run_background(
            "diagnostics",
            "Running the deep checks" if deep else "Running the checks",
            lambda: self.app.diagnostics.run(deep=deep),
            on_success=done,
            busy_widgets=[self.run_button, self.deep_button],
            status=self.summary,
        )

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._report_text)
        self.summary.setText(self.summary.text() + "  (report copied to the clipboard)")
