import ipaddress
import os
import sys

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

import windows_ics


def get_running_path(relative_path):
    if os.path.exists(relative_path):
        return relative_path
    if getattr(sys, "frozen", False):
        base_directory = os.path.join(os.path.dirname(sys.executable), "_internal")
    else:
        base_directory = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(base_directory, relative_path)
    return candidate if os.path.exists(candidate) else relative_path


def parse_ipv4(text):
    try:
        return ipaddress.IPv4Address(text.strip())
    except ipaddress.AddressValueError:
        raise ValueError(f"Invalid IPv4 address: {text.strip() or '(empty)'}") from None


def parse_dns_servers(text):
    servers = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part:
            servers.append(str(parse_ipv4(part)))
    return servers


def example_client_address(server, prefix_length):
    network = ipaddress.IPv4Network(f"{server}/{prefix_length}", strict=False)
    for offset in (100, 10, 2, 1):
        try:
            candidate = network.network_address + offset
        except ipaddress.AddressValueError:
            continue
        if candidate in network and candidate != server:
            return candidate
    return None


class Worker(QThread):
    line = Signal(str)
    done = Signal(bool, str)

    def __init__(self, task):
        super().__init__()
        self._task = task

    def run(self):
        try:
            self._task(self.line.emit)
        except Exception as error:
            self.done.emit(False, str(error))
        else:
            self.done.emit(True, "")


class IcsManager(QMainWindow):
    def __init__(self):
        super().__init__()
        with open(get_running_path("version.txt"), "r") as version_file:
            version = version_file.read().strip()
        self.setWindowTitle(f"Windows ICS Manager V{version}")
        self.resize(1020, 800)
        self.setWindowIcon(QIcon(get_running_path("icon.ico")))

        self._busy = False
        self._workers = []
        self._adapters = []
        self._sharing = []
        self._share_ip_touched = False
        self._client_ip_touched = False
        self._metric_spins = {}
        self._metric_originals = {}

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        top_layout = QHBoxLayout()
        self.status_label = QLabel("Loading network information...")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_network)
        top_layout.addWidget(self.status_label, 1)
        top_layout.addWidget(self.refresh_button)
        main_layout.addLayout(top_layout)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_share_tab(), "Share Internet (Server)")
        self.tabs.addTab(self._build_client_tab(), "Configure Client")
        self.tabs.addTab(self._build_metrics_tab(), "Adapter Metrics")
        main_layout.addWidget(self.tabs, 3)

        log_group = QGroupBox("Output")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log_view)
        main_layout.addWidget(log_group, 2)

        self.statusBar().showMessage("Ready")
        QTimer.singleShot(0, self.refresh_network)

    def _build_share_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form_layout = QFormLayout()
        self.public_combo = QComboBox()
        self.private_combo = QComboBox()
        self.share_ip_edit = QLineEdit("192.168.137.1")
        self.share_ip_edit.textEdited.connect(self._on_share_ip_edited)
        self.prefix_spin = QSpinBox()
        self.prefix_spin.setRange(8, 30)
        self.prefix_spin.setValue(24)
        self.prefix_spin.valueChanged.connect(self._update_share_hint)
        form_layout.addRow("Adapter with internet (public side):", self.public_combo)
        form_layout.addRow("Adapter to share on (private side):", self.private_combo)
        form_layout.addRow("IP for the shared adapter:", self.share_ip_edit)
        form_layout.addRow("Prefix length:", self.prefix_spin)
        layout.addLayout(form_layout)

        self.private_combo.currentIndexChanged.connect(self._suggest_share_ip)

        button_layout = QHBoxLayout()
        self.enable_button = QPushButton("Enable Internet Sharing")
        self.enable_button.clicked.connect(self.enable_sharing)
        self.disable_button = QPushButton("Disable All Sharing")
        self.disable_button.clicked.connect(self.disable_sharing)
        button_layout.addWidget(self.enable_button)
        button_layout.addWidget(self.disable_button)
        button_layout.addStretch(1)
        layout.addLayout(button_layout)

        self.sharing_label = QLabel("No adapter is currently sharing an internet connection.")
        self.sharing_label.setWordWrap(True)
        layout.addWidget(self.sharing_label)

        self.share_hint_label = QLabel("")
        self.share_hint_label.setWordWrap(True)
        self.share_hint_label.setStyleSheet("color: #555;")
        layout.addWidget(self.share_hint_label)

        layout.addStretch(1)
        return widget

    def _build_client_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form_layout = QFormLayout()
        self.client_adapter_combo = QComboBox()
        self.server_ip_edit = QLineEdit()
        self.server_ip_edit.setPlaceholderText("e.g. 192.168.137.1")
        self.server_ip_edit.textEdited.connect(self._on_server_ip_edited)
        self.client_ip_edit = QLineEdit()
        self.client_ip_edit.setPlaceholderText("e.g. 192.168.137.100")
        self.client_ip_edit.textEdited.connect(self._on_client_ip_edited)
        self.client_prefix_spin = QSpinBox()
        self.client_prefix_spin.setRange(8, 30)
        self.client_prefix_spin.setValue(24)
        self.client_prefix_spin.valueChanged.connect(self._suggest_client_ip)
        self.dns_edit = QLineEdit()
        self.dns_edit.setPlaceholderText("Defaults to the server IP; separate several with commas")
        form_layout.addRow("Adapter connected to the sharing PC:", self.client_adapter_combo)
        form_layout.addRow("Server (gateway) IP:", self.server_ip_edit)
        form_layout.addRow("Client IP:", self.client_ip_edit)
        form_layout.addRow("Prefix length:", self.client_prefix_spin)
        form_layout.addRow("DNS servers:", self.dns_edit)
        layout.addLayout(form_layout)

        button_layout = QHBoxLayout()
        self.apply_client_button = QPushButton("Apply Static Configuration")
        self.apply_client_button.clicked.connect(self.apply_client_config)
        self.dhcp_button = QPushButton("Return Adapter to DHCP")
        self.dhcp_button.clicked.connect(self.set_client_dhcp)
        self.diagnose_button = QPushButton("Run Diagnostics")
        self.diagnose_button.clicked.connect(self.run_diagnostics)
        button_layout.addWidget(self.apply_client_button)
        button_layout.addWidget(self.dhcp_button)
        button_layout.addWidget(self.diagnose_button)
        button_layout.addStretch(1)
        layout.addLayout(button_layout)

        self.diagnostics_label = QLabel("Diagnostics have not been run yet.")
        self.diagnostics_label.setTextFormat(Qt.RichText)
        self.diagnostics_label.setWordWrap(True)
        layout.addWidget(self.diagnostics_label)

        layout.addStretch(1)
        return widget

    def _build_metrics_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        hint = QLabel(
            "The interface metric decides which adapter is preferred when Windows chooses a route; "
            "lower values are preferred. Applying a metric disables automatic metric selection for that adapter."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #555;")
        layout.addWidget(hint)

        self.metrics_table = QTableWidget(0, 5)
        self.metrics_table.setHorizontalHeaderLabels(["Adapter", "Description", "Status", "IPv4 addresses", "Metric"])
        self.metrics_table.verticalHeader().setVisible(False)
        self.metrics_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.metrics_table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.metrics_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.metrics_table, 1)

        button_layout = QHBoxLayout()
        self.apply_metrics_button = QPushButton("Apply Metrics")
        self.apply_metrics_button.clicked.connect(self.apply_metrics)
        button_layout.addWidget(self.apply_metrics_button)
        button_layout.addStretch(1)
        layout.addLayout(button_layout)

        self.metrics_label = QLabel("Adapter metrics have not been loaded yet.")
        self.metrics_label.setWordWrap(True)
        layout.addWidget(self.metrics_label)

        return widget

    def apply_metrics(self):
        changes = []
        for name, spin in self._metric_spins.items():
            original = self._metric_originals.get(name)
            if original is None:
                continue
            if spin.value() != original:
                changes.append((name, spin.value(), original))
        if not changes:
            QMessageBox.information(self, "No changes", "No interface metric was modified.")
            return
        descriptions = "\n".join(f"{name}: {old} -> {new}" for name, new, old in changes)
        answer = QMessageBox.question(
            self,
            "Apply interface metrics",
            f"Apply the following interface metric changes?\n\n{descriptions}",
        )
        if answer != QMessageBox.Yes:
            return
        snapshot = {}

        def task(log):
            for name, new_metric, _old in changes:
                windows_ics.set_interface_metric(name, new_metric, on_output=log)
            snapshot.update(windows_ics.get_network_snapshot())

        self._run_task("Updating interface metrics...", task, lambda: self._apply_snapshot(snapshot))

    def refresh_network(self):
        if self._busy:
            return
        snapshot = {}

        def task(log):
            snapshot.update(windows_ics.get_network_snapshot())

        self._run_task("Reading network configuration...", task, lambda: self._apply_snapshot(snapshot))

    def _apply_snapshot(self, snapshot):
        self._adapters = snapshot.get("Adapters") or []
        if isinstance(self._adapters, dict):
            self._adapters = [self._adapters]
        self._sharing = snapshot.get("Sharing") or []
        if isinstance(self._sharing, dict):
            self._sharing = [self._sharing]

        private_selection = self._selected_adapter(self.private_combo)
        client_selection = self._selected_adapter(self.client_adapter_combo)
        self._fill_adapter_combo(self.public_combo, preferred=snapshot.get("DefaultRouteAlias"))
        self._fill_adapter_combo(self.private_combo, preferred=private_selection)
        self._fill_adapter_combo(self.client_adapter_combo, preferred=client_selection)

        enabled = [item for item in self._sharing if item.get("SharingEnabled")]
        if enabled:
            descriptions = []
            for item in enabled:
                role = "public / internet source" if item.get("ConnectionType") == 0 else "private / shared LAN"
                descriptions.append(f"{item.get('Name', '?')} ({role})")
            self.sharing_label.setText("Currently sharing: " + "; ".join(descriptions))
        else:
            self.sharing_label.setText("No adapter is currently sharing an internet connection.")

        internet_source = snapshot.get("DefaultRouteAlias")
        status = f"Adapters: {len(self._adapters)}"
        if internet_source:
            status += f" | Internet source: {internet_source}"
        self.status_label.setText(status)

        self._suggest_share_ip()
        self._update_share_hint()
        self._suggest_client_ip()
        self._populate_metrics()

    def _populate_metrics(self):
        self.metrics_table.setRowCount(0)
        self._metric_spins = {}
        self._metric_originals = {}
        adapters = sorted(
            self._adapters,
            key=lambda adapter: (
                adapter.get("Metric") is None,
                adapter.get("Metric") if adapter.get("Metric") is not None else 0,
            ),
        )
        for adapter in adapters:
            row = self.metrics_table.rowCount()
            self.metrics_table.insertRow(row)
            name = adapter.get("Name", "?")
            addresses = adapter.get("IPv4") or []
            if isinstance(addresses, str):
                addresses = [addresses]
            self.metrics_table.setItem(row, 0, QTableWidgetItem(name))
            self.metrics_table.setItem(row, 1, QTableWidgetItem(str(adapter.get("Description", ""))))
            self.metrics_table.setItem(row, 2, QTableWidgetItem(str(adapter.get("Status", "?"))))
            self.metrics_table.setItem(row, 3, QTableWidgetItem(", ".join(addresses) or "-"))
            metric = adapter.get("Metric")
            spin = QSpinBox()
            spin.setRange(1, 9999)
            if isinstance(metric, int) and metric > 9999:
                spin.setMaximum(metric)
            if isinstance(metric, int) and metric >= 1:
                spin.setValue(metric)
            else:
                spin.setValue(1)
                spin.setEnabled(False)
            if adapter.get("AutomaticMetric") == "Enabled" and metric is not None:
                spin.setToolTip("This adapter currently uses an automatic metric.")
            self.metrics_table.setCellWidget(row, 4, spin)
            self._metric_spins[name] = spin
            if metric is not None:
                self._metric_originals[name] = int(metric)
        self.metrics_label.setText(
            f"{len(self._metric_spins)} adapter(s) loaded. Edit a metric and press Apply Metrics to change it."
        )

    def _fill_adapter_combo(self, combo, preferred=None):
        current = self._selected_adapter(combo) or preferred
        combo.blockSignals(True)
        combo.clear()
        for adapter in self._adapters:
            addresses = adapter.get("IPv4") or []
            if isinstance(addresses, str):
                addresses = [addresses]
            summary = ", ".join(addresses) if addresses else "-"
            label = f"{adapter.get('Name', '?')} | {adapter.get('Status', '?')} | {adapter.get('Description', '')} | {summary}"
            combo.addItem(label, adapter.get("Name"))
        if current:
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _selected_adapter(self, combo):
        return combo.currentData()

    def _adapter_by_name(self, name):
        for adapter in self._adapters:
            if adapter.get("Name") == name:
                return adapter
        return None

    def _on_share_ip_edited(self, _text):
        self._share_ip_touched = True
        self._update_share_hint()

    def _update_share_hint(self):
        try:
            share_ip = ipaddress.IPv4Address(self.share_ip_edit.text().strip())
            prefix = self.prefix_spin.value()
            example = example_client_address(share_ip, prefix)
            if example is not None:
                self.share_hint_label.setText(
                    f"Clients must use {share_ip} as gateway and DNS. Example client address: {example}/{prefix}."
                )
                return
        except ipaddress.AddressValueError:
            pass
        self.share_hint_label.setText("Clients must use the shared IP as gateway and DNS.")

    def _suggest_share_ip(self):
        if self._share_ip_touched:
            return
        suggestion = windows_ics.DEFAULT_ICS_SCOPE
        adapter = self._adapter_by_name(self._selected_adapter(self.private_combo))
        if adapter:
            addresses = adapter.get("IPv4") or []
            if isinstance(addresses, str):
                addresses = [addresses]
            if addresses:
                octets = addresses[0].split(".")
                if len(octets) == 4:
                    suggestion = ".".join(octets[:3] + ["10"])
        self.share_ip_edit.setText(suggestion)
        self._update_share_hint()

    def _on_server_ip_edited(self, _text):
        self._suggest_client_ip()

    def _on_client_ip_edited(self, _text):
        self._client_ip_touched = True

    def _suggest_client_ip(self):
        if self._client_ip_touched:
            return
        server_text = self.server_ip_edit.text().strip()
        if not server_text:
            return
        try:
            server = ipaddress.IPv4Address(server_text)
        except ipaddress.AddressValueError:
            return
        candidate = example_client_address(server, self.client_prefix_spin.value())
        if candidate is not None:
            self.client_ip_edit.setText(str(candidate))

    def _validate_share_inputs(self):
        public = self._selected_adapter(self.public_combo)
        private = self._selected_adapter(self.private_combo)
        if not public or not private:
            QMessageBox.warning(self, "Missing adapters", "Select both the internet adapter and the adapter to share on.")
            return None
        if public == private:
            QMessageBox.warning(self, "Invalid selection", "The public and private adapters must be different.")
            return None
        try:
            share_ip = parse_ipv4(self.share_ip_edit.text())
        except ValueError as error:
            QMessageBox.warning(self, "Invalid IP address", str(error))
            return None
        prefix = self.prefix_spin.value()
        share_network = ipaddress.IPv4Network(f"{share_ip}/{prefix}", strict=False)
        public_adapter = self._adapter_by_name(public) or {}
        addresses = public_adapter.get("IPv4") or []
        if isinstance(addresses, str):
            addresses = [addresses]
        conflicts = [address for address in addresses if ipaddress.IPv4Address(address) in share_network]
        if conflicts:
            answer = QMessageBox.warning(
                self,
                "Subnet conflict",
                f"{share_ip}/{prefix} is in the same subnet as the internet adapter address(es) "
                f"{', '.join(conflicts)}.\n\nContinue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return None
        return public, private, str(share_ip), prefix

    def enable_sharing(self):
        selection = self._validate_share_inputs()
        if selection is None:
            return
        public, private, share_ip, prefix = selection
        answer = QMessageBox.question(
            self,
            "Enable internet sharing",
            f'Share the internet connection from "{public}" to "{private}" using {share_ip}/{prefix}?',
        )
        if answer != QMessageBox.Yes:
            return
        snapshot = {}

        def task(log):
            windows_ics.enable_sharing(public, private, share_ip, prefix, on_output=log)
            snapshot.update(windows_ics.get_network_snapshot())

        self._run_task(f"Enabling sharing on {private}...", task, lambda: self._apply_snapshot(snapshot))

    def disable_sharing(self):
        answer = QMessageBox.question(
            self,
            "Disable sharing",
            "Disable Internet Connection Sharing on all adapters and reset the ICS scope to 192.168.137.1?",
        )
        if answer != QMessageBox.Yes:
            return
        snapshot = {}

        def task(log):
            windows_ics.disable_all_sharing(on_output=log)
            snapshot.update(windows_ics.get_network_snapshot())

        self._run_task("Disabling Internet Connection Sharing...", task, lambda: self._apply_snapshot(snapshot))

    def apply_client_config(self):
        adapter = self._selected_adapter(self.client_adapter_combo)
        if not adapter:
            QMessageBox.warning(self, "Missing adapter", "Select the client adapter.")
            return
        try:
            server_ip = parse_ipv4(self.server_ip_edit.text())
            client_ip = parse_ipv4(self.client_ip_edit.text())
        except ValueError as error:
            QMessageBox.warning(self, "Invalid IP address", str(error))
            return
        prefix = self.client_prefix_spin.value()
        try:
            dns_servers = parse_dns_servers(self.dns_edit.text())
        except ValueError as error:
            QMessageBox.warning(self, "Invalid DNS server", str(error))
            return
        server_network = ipaddress.IPv4Network(f"{server_ip}/{prefix}", strict=False)
        if client_ip not in server_network:
            answer = QMessageBox.warning(
                self,
                "Different subnets",
                f"{server_ip} and {client_ip} are not in the same /{prefix} subnet.\n\nContinue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        dns_argument = ",".join(dns_servers)
        dns_display = dns_argument or str(server_ip)
        answer = QMessageBox.question(
            self,
            "Apply static configuration",
            f'Configure "{adapter}" as {client_ip}/{prefix}, gateway {server_ip}, DNS {dns_display}?',
        )
        if answer != QMessageBox.Yes:
            return
        diagnosis = {}

        def task(log):
            windows_ics.configure_client(adapter, str(server_ip), str(client_ip), prefix, dns_argument, on_output=log)
            result, lines = windows_ics.test_connectivity(str(server_ip))
            for line in lines:
                log(line)
            diagnosis.update(result)

        self._run_task(f"Configuring {adapter}...", task, lambda: self._show_diagnostics(diagnosis))

    def set_client_dhcp(self):
        adapter = self._selected_adapter(self.client_adapter_combo)
        if not adapter:
            QMessageBox.warning(self, "Missing adapter", "Select the client adapter.")
            return
        answer = QMessageBox.question(
            self,
            "Return to DHCP",
            f'Enable DHCP and remove the static IP configuration from "{adapter}"?',
        )
        if answer != QMessageBox.Yes:
            return
        snapshot = {}

        def task(log):
            windows_ics.set_client_dhcp(adapter, on_output=log)
            snapshot.update(windows_ics.get_network_snapshot())

        self._run_task(f"Returning {adapter} to DHCP...", task, lambda: self._apply_snapshot(snapshot))

    def run_diagnostics(self):
        try:
            server_ip = parse_ipv4(self.server_ip_edit.text())
        except ValueError as error:
            QMessageBox.warning(self, "Invalid IP address", str(error))
            return
        diagnosis = {}

        def task(log):
            result, lines = windows_ics.test_connectivity(str(server_ip))
            for line in lines:
                log(line)
            diagnosis.update(result)

        self._run_task(f"Testing connectivity to {server_ip}...", task, lambda: self._show_diagnostics(diagnosis))

    def _show_diagnostics(self, diagnosis):
        checks = (
            (f"Ping server {self.server_ip_edit.text().strip()}", diagnosis.get("Server")),
            ("Ping internet 1.1.1.1", diagnosis.get("Internet")),
            ("DNS lookup", diagnosis.get("Dns")),
        )
        parts = []
        for label, ok in checks:
            color = "#2e7d32" if ok else "#c62828"
            status = "OK" if ok else "FAILED"
            parts.append(f'<span style="color:{color}">&#9679; {label}: {status}</span>')
        self.diagnostics_label.setText("<br>".join(parts))

    def _run_task(self, description, task, on_success=None):
        if self._busy:
            return
        self._log(description)
        self.statusBar().showMessage(description)
        self._set_busy(True)
        worker = Worker(task)
        worker.line.connect(self._log)
        worker.done.connect(lambda ok, error: self._task_done(worker, ok, error, on_success))
        self._workers.append(worker)
        worker.start()

    def _task_done(self, worker, ok, error, on_success):
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()
        self._set_busy(False)
        if ok:
            self.statusBar().showMessage("Done", 5000)
            if on_success is not None:
                on_success()
        else:
            self.statusBar().showMessage("Failed", 5000)
            self._log("ERROR: " + error)
            QMessageBox.critical(self, "Operation failed", error)

    def _set_busy(self, busy):
        self._busy = busy
        for widget in (
            self.refresh_button,
            self.enable_button,
            self.disable_button,
            self.apply_client_button,
            self.dhcp_button,
            self.diagnose_button,
            self.public_combo,
            self.private_combo,
            self.share_ip_edit,
            self.prefix_spin,
            self.client_adapter_combo,
            self.server_ip_edit,
            self.client_ip_edit,
            self.client_prefix_spin,
            self.dns_edit,
            self.metrics_table,
            self.apply_metrics_button,
        ):
            widget.setEnabled(not busy)

    def _log(self, text):
        text = text.rstrip()
        if not text:
            return
        self.log_view.appendPlainText(text)

    def closeEvent(self, event):
        if any(worker.isRunning() for worker in self._workers):
            QMessageBox.information(self, "Operation in progress", "Please wait for the current operation to finish.")
            event.ignore()
            return
        super().closeEvent(event)


def main():
    if not windows_ics.is_admin():
        if "--relaunched" not in sys.argv and windows_ics.relaunch_as_admin(["--relaunched"]):
            return 0
        app = QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "Administrator rights required",
            "Windows ICS Manager needs administrator rights to change network settings.",
        )
        return 1
    app = QApplication(sys.argv)
    window = IcsManager()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
