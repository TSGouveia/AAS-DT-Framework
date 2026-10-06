import sys
import os
import collections
import collections.abc
import fractions
import math

# =================================================================
# COMPATIBILITY PATCHES (Immediate)
# =================================================================
def apply_compatibility_patches():
    """Applies patches for Python 3.10+ and older library versions."""
    for name in ['Mapping', 'MutableMapping', 'Set', 'MutableSet', 'Iterable', 'MutableSequence', 'Sequence']:
        if not hasattr(collections, name):
            obj = getattr(collections.abc, name)
            setattr(collections, name, obj)
            if 'collections' in sys.modules:
                setattr(sys.modules['collections'], name, obj)

    if not hasattr(fractions, 'gcd'): fractions.gcd = math.gcd

    try:
        import numpy as np
        np_patches = {'float': float, 'float_': np.float64, 'int': int, 'int_': np.int64, 'bool': bool}
        for old_attr, replacement in np_patches.items():
            if not hasattr(np, old_attr): setattr(np, old_attr, replacement)
    except ImportError: pass

    try:
        import networkx.utils as nx_utils
        import networkx as nx
        nx.utils = nx_utils
        import networkx.algorithms.bipartite
        import networkx.generators.intersection
    except: pass

apply_compatibility_patches()

from urdfpy import URDF

import io
import re
import json
import time
import base64
import hashlib
import posixpath
import mimetypes
import traceback
import xml.etree.ElementTree as ET
from xml.dom import minidom

import trimesh
import numpy as np
import pyvista as pv
import requests
import xacro
import xacro.substitution_args
from pyvistaqt import BackgroundPlotter

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QLineEdit, 
                             QFileDialog, QStackedWidget, QFormLayout, 
                             QTreeWidget, QTreeWidgetItem, QCheckBox, 
                             QDoubleSpinBox, QSpinBox, QComboBox, QMessageBox, QTextEdit,
                             QGroupBox, QScrollArea, QDialog, QDialogButtonBox,
                             QSplitter, QListWidget, QListWidgetItem, QInputDialog,
                             QGridLayout, QAbstractItemView)
from PyQt6.QtCore import Qt, QSize, QTimer, QEvent
from PyQt6.QtGui import QColor, QFont

import basyx.aas.model as model
import basyx.aas.adapter.aasx as aasx

# =================================================================
# XACRO RESOLVER & ENGINE
# =================================================================
class XacroResolver:
    def __init__(self, root_path):
        self.root_path = os.path.abspath(root_path).replace('\\', '/')
        self.file_map = {} # full_path.lower() -> full_path
        self.basename_map = {} # basename.lower() -> [full_paths]
        print(f"[RESOLVER] Mapping: {self.root_path}")
        self._index_directory(self.root_path)
        
        # Also index siblings of root_path to find other packages in the same workspace
        parent = os.path.dirname(self.root_path)
        if parent and os.path.isdir(parent):
            print(f"[RESOLVER] Indexing sibling packages in: {parent}")
            for d in os.listdir(parent):
                d_path = os.path.join(parent, d)
                if os.path.isdir(d_path) and d_path.replace('\\', '/') != self.root_path:
                    self._index_directory(d_path)
                    
        print(f"[RESOLVER] {len(self.file_map)} entries mapped.")

    def _index_directory(self, path):
        for r, d, fs in os.walk(path):
            for f in fs:
                full_path = os.path.abspath(os.path.join(r, f)).replace('\\', '/')
                self.file_map[full_path.lower()] = full_path
                bn = f.lower()
                if bn not in self.basename_map: self.basename_map[bn] = []
                if full_path not in self.basename_map[bn]:
                    self.basename_map[bn].append(full_path)

    def add_search_path(self, new_path):
        print(f"[RESOLVER] Adding search path: {new_path}")
        old_count = len(self.file_map)
        self._index_directory(new_path)
        print(f"[RESOLVER] {len(self.file_map) - old_count} more files added.")

    def resolve(self, path):
        if not path: return None
        p_orig = path
        p_lower = path.lower().replace('\\', '/')
        
        # 1. Direct hit (absolute or relative to CWD)
        if p_lower in self.file_map: 
            return self.file_map[p_lower]
        
        # Normalize and resolve package directory
        if re.match(r'^\$\(find [^)]+\)$', path):
            pkg_name = re.search(r'\$\(find ([^)]+)\)', path).group(1).lower()
            # Try to find a directory with this name
            for r, ds, fs in os.walk(self.root_path):
                for d in ds:
                    if d.lower() == pkg_name:
                        return os.path.abspath(os.path.join(r, d)).replace('\\', '/')
            return None

        normalized = re.sub(r'\$\(find [^)]+\)', '', path.replace('package://', ''))
        normalized = '/'.join([s for s in normalized.replace('\\', '/').split('/') if s and s != '.'])
        norm_lower = normalized.lower()
        
        # 3. Best suffix match
        best_match = None
        max_score = -1
        
        bn = os.path.basename(normalized).lower()
        candidates = self.basename_map.get(bn, [])
        
        for cand in candidates:
            cand_lower = cand.lower()
            if cand_lower.endswith(norm_lower):
                score = len(norm_lower)
                if score > max_score:
                    max_score = score
                    best_match = cand
            else:
                # Try partial suffix match (e.g. if package name is different)
                parts = norm_lower.split('/')
                for i in range(len(parts)-1, 0, -1):
                    sub = '/'.join(parts[i:])
                    if cand_lower.endswith(sub):
                        score = len(sub)
                        if score > max_score:
                            max_score = score
                            best_match = cand
                        break
        
        if best_match:
            return best_match
            
        return None

_original_abs_filename_spec = None
_original_parse = None
_active_resolver = None

def _ensure_xacro_patched():
    global _original_abs_filename_spec, _original_parse
    if _original_abs_filename_spec is not None: return
    
    _original_abs_filename_spec = xacro.abs_filename_spec
    _original_parse = xacro.parse

    def patched_abs_filename_spec(filename_spec):
        if _active_resolver:
            resolved = _active_resolver.resolve(filename_spec)
            if resolved: return resolved
        return _original_abs_filename_spec(filename_spec)

    def patched_parse(inp, filename=None):
        if inp is None and filename and _active_resolver:
            resolved = _active_resolver.resolve(filename)
            if resolved:
                print(f"[XACRO-PATCH] Reading resolved file: {resolved}")
                with open(resolved, 'r', encoding='utf-8') as f: content = f.read()
                return minidom.parseString(content)
        return _original_parse(inp, filename)

    xacro.abs_filename_spec = patched_abs_filename_spec
    xacro.parse = patched_parse

    try:
        xacro.substitution_args._eval_find = lambda pkg: ""
        print("[XACRO-PATCH] Successfully patched xacro.substitution_args._eval_find")
    except Exception as e:
        print(f"[XACRO-PATCH] Warning: Failed to patch substitution_args: {e}")

def expand_xacro_final(filepath, mappings, resolver):
    _ensure_xacro_patched()
    print(f"[XACRO] Expanding {filepath} with mappings {mappings}")
    global _active_resolver
    _active_resolver = resolver
    try:
        doc = xacro.process_file(filepath, mappings=mappings)
        print(f"[XACRO] Expansion completed successfully.")
        return doc.toxml()
    except Exception as e:
        print(f"[XACRO] ERROR during expansion: {e}")
        raise e
    finally: _active_resolver = None

# =================================================================
# ASSET MANAGER
# =================================================================
class AssetManager:
    def __init__(self, assets_root):
        self.assets_root = os.path.abspath(assets_root)
        self.files_to_upload = {}
        self.exts = ['.stl', '.dae', '.obj', '.mtl', '.jpg', '.png', '.jpeg', '.yaml', '.json', '.txt', '.conf']
        print(f"[ASSETS] AssetManager started at: {self.assets_root}")

    def collect(self, filename, resolver):
        if not filename: return None
        print(f"[ASSETS] Collecting: {filename}")
        found = resolver.resolve(filename)
        if not found: 
            print(f"   [ASSETS] WARNING: Failed to resolve '{filename}' for collection.")
            return None
        found = os.path.abspath(found)
        if found in self.files_to_upload: 
            print(f"   [ASSETS] Already in cache: {self.files_to_upload[found]}")
            return self.files_to_upload[found]
        
        rel_path = os.path.relpath(found, self.assets_root).replace(os.sep, '/')
        self.files_to_upload[found] = rel_path
        print(f"   [ASSETS] New asset: {found} -> {rel_path}")
        
        if found.lower().endswith(('.dae', '.obj', '.mtl', '.yaml')):
            print(f"   [ASSETS] Analyzing dependencies of {os.path.basename(found)}...")
            try:
                with open(found, 'r', errors='ignore', encoding='utf-8') as f: content = f.read()
                refs = re.findall(r'<(?:init_from|texture|map_Kd|mtllib)[^>]*>\s*([^< \r\n]+)|(?:map_\w+|mtllib|import)\s+([^\s\r\n]+)', content)
                for r in refs:
                    ref = (r[0] or r[1]).strip()
                    if ref and any(ext in ref.lower() for ext in self.exts):
                        print(f"      [ASSETS] Reference found: {ref}")
                        self.collect(ref, resolver)
            except Exception as e:
                print(f"      [ASSETS] Error reading {found}: {e}")
        return rel_path

# =================================================================
# DIALOGS & DEPENDENCY MANAGER
# =================================================================
class DependencyDialog(QDialog):
    def __init__(self, missing_files, resolver, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Missing Files")
        self.setMinimumWidth(500)
        self.resolver, self.missing = resolver, missing_files
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Files not found:</b>"))
        self.list_widget = QListWidget()
        for f in self.missing: self.list_widget.addItem(f"❌ {f}")
        layout.addWidget(self.list_widget)
        btn_add = QPushButton("📂 Add Folder"); btn_add.clicked.connect(self.add_folder)
        self.btn_retry = QPushButton("Try Again"); self.btn_retry.setEnabled(False); self.btn_retry.clicked.connect(self.accept)
        btn_layout = QHBoxLayout(); btn_layout.addWidget(btn_add); btn_layout.addWidget(self.btn_retry)
        layout.addLayout(btn_layout)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Folder")
        if path:
            self.resolver.add_search_path(path)
            all_ok = True
            for i in range(self.list_widget.count()):
                it = self.list_widget.item(i)
                orig = it.text().replace("❌ ", "").replace("✅ ", "")
                if self.resolver.resolve(orig): it.setText(f"✅ {orig}"); it.setForeground(Qt.GlobalColor.green)
                else: all_ok = False
            if all_ok: self.btn_retry.setEnabled(True)

class XacroArgsDialog(QDialog):
    def __init__(self, args_info, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Xacro Variables")
        self.setMinimumWidth(600)
        self.results = {}
        
        main_layout = QVBoxLayout(self)
        self.grid = QGridLayout()
        self.grid.setSpacing(10)
        
        self.inputs = {}
        cols = 2
        for i, (name, default) in enumerate(args_info.items()):
            row = i // cols
            col_offset = (i % cols) * 2
            
            lbl = QLabel(f"<b>{name}</b>")
            
            edit = QLineEdit()
            if default: edit.setPlaceholderText(str(default))
            edit.setMinimumWidth(120)
            
            self.inputs[name] = edit
            self.grid.addWidget(lbl, row, col_offset)
            self.grid.addWidget(edit, row, col_offset + 1)

        main_layout.addLayout(self.grid)
        
        btn = QPushButton("Confirm Mappings"); btn.clicked.connect(self.accept_all)
        btn.setStyleSheet("background: #2980b9; color: white; padding: 10px; font-weight: bold; margin-top: 10px;")
        main_layout.addWidget(btn)

    def accept_all(self):
        for n, e in self.inputs.items():
            v = e.text().strip()
            if v: self.results[n] = v
        self.accept()

class DependencyManager:
    @staticmethod
    def scan_xacro_args(filepath, resolver):
        all_defs = {}
        all_text = ""
        visited = set()
        
        def collect_recursive(fp):
            nonlocal all_text
            if fp in visited: return
            visited.add(fp)
            print(f"[DEP-MANAGER] Analyzing: {fp}")
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    content = f.read()
                    all_text += "\n" + content
                    
                    for m in re.finditer(r'<xacro:arg\s+name=["\']([^"\']+)["\'](?:\s+default=["\']([^"\']+)["\'])?', content):
                        name, default = m.group(1), m.group(2)
                        
                        if default and '$(find ' in default:
                            def replacer(match):
                                pkg = match.group(1)
                                res = resolver.resolve(f"$(find {pkg})") or f"[{pkg}]"
                                return res
                            default = re.sub(r'\$\(find ([^)]+)\)', replacer, default)
                        
                        all_defs[name] = default

                    for inc in re.findall(r'<xacro:include\s+filename=["\']([^"\']+)["\']', content):
                        res = resolver.resolve(inc)
                        if res: collect_recursive(res)
            except Exception as e:
                print(f"   [DEP-MANAGER] Error reading {fp}: {e}")

        collect_recursive(filepath)
        used_names = set(re.findall(r'\$\(arg\s+([^)]+)\)', all_text))
        filtered_args = {k: v for k, v in all_defs.items() if k in used_names}
        print(f"[DEP-MANAGER] Found {len(all_defs)} definitions, {len(filtered_args)} are actually used.")
        return filtered_args

    @staticmethod
    def scan_meshes(xml_text):
        meshes = list(set(re.findall(r'<mesh\s+filename=["\']([^"\']+)["\']', xml_text)))
        print(f"[DEP-MANAGER] Meshes detected in XML: {len(meshes)}")
        return meshes

    @staticmethod
    def check_recursive_xacro(filepath, resolver, visited=None):
        if visited is None: visited = set()
        if filepath in visited: return []
        missing = []
        print(f"[DEP-MANAGER] Recursive Xacro check: {filepath}")
        try:
            with open(filepath, 'r', encoding='utf-8') as f: content = f.read()
            for inc in re.findall(r'<xacro:include\s+filename=["\']([^"\']+)["\']', content):
                res = resolver.resolve(inc)
                if not res: 
                    print(f"   [DEP-MANAGER] MISSING: {inc}")
                    missing.append(inc)
                else: missing.extend(DependencyManager.check_recursive_xacro(res, resolver, visited))
        except Exception as e:
            print(f"   [DEP-MANAGER] Error verifying {filepath}: {e}")
        return list(set(missing))

# =================================================================
# AAS UTILITIES
# =================================================================
def get_file_rel_path(el):
    val = el.get("value", "")
    id_short = el.get("idShort", "")
    
    rel_path = None
    # 1. First priority: decode path from idShort if it is encoded as PATH_...
    if id_short.startswith("PATH_"):
        encoded_path = id_short[len("PATH_"):]
        rel_path = encoded_path.replace("_SL_", "/").replace("_DOT_", ".").replace("_DASH_", "-")
        
    # 2. Second priority: strip standard prefix /aasx/robot/ or /aasx/kit/ if present
    if not rel_path:
        for prefix in ["/aasx/robot/", "/aasx/kit/", "/aasx/", "aasx/", "robot/", "kit/"]:
            if val.startswith(prefix):
                rel_path = val[len(prefix):]
                break
            
    # 3. Fallback: return basename of value, or the value itself if it contains no slash
    if not rel_path:
        if "/" in val or "\\" in val:
            rel_path = val.replace("\\", "/")
        else:
            rel_path = os.path.basename(val)
            
    # Strip any leading slashes to prevent os.path.join from treating it as drive-root relative
    while rel_path.startswith("/"):
        rel_path = rel_path[1:]
    return rel_path

class RobustFileRepo(dict):
    def get_content_type(self, fn): return mimetypes.guess_type(fn)[0] or "application/octet-stream"
    def add_file(self, n, c): self[n] = c
    def write_file(self, fn, t):
        if fn in self: t.write(self[fn])
    def get_sha256(self, fn):
        if fn in self: return hashlib.sha256(self[fn]).hexdigest()
        return ""

def encode_id(identifier):
    return base64.urlsafe_b64encode(identifier.encode()).decode().rstrip("=")

def sanitize_aas_id(name):
    res = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    res = re.sub(r'_+', '_', res)
    if not res: return "ELEMENT_ID"
    if not res[0].isalpha(): res = "ID_" + res
    return res.strip('_')

def is_valid_aas_id(name):
    if not name: return False
    return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', name))

ERROR_STYLE = "background-color: #4a1010; border: 1px solid #e74c3c;"
VALID_STYLE = "background-color: #2d2d2d; border: 1px solid #3d3d3d;"
VALIDATION_MSG = "⚠️ Invalid ID: Letters, digits, underscores only. Must start with letter."

class ActuatorWidget(QGroupBox):
    def __init__(self, parent_wizard):
        super().__init__("Actuator Configuration")
        self.wizard = parent_wizard
        self.layout = QFormLayout(self); self.layout.setSpacing(5); self.layout.setContentsMargins(10, 15, 10, 10)
        
        self.edit_pin = QLineEdit("IN_Pin")
        self.edit_pin.textChanged.connect(self.validate)
        
        self.edit_node = QLineEdit("")
        self.edit_node.setReadOnly(True)
        self.edit_node.setPlaceholderText("Select node in Tree or 3D...")
        
        self.combo_stop_sens = QComboBox()
        self.combo_stop_sens.setEditable(True)
        
        self.check_visible = QCheckBox("Visible by Default")
        self.check_visible.setChecked(True)
        
        self.combo_behavior = QComboBox(); self.combo_behavior.addItems([
            "Conveyor", "RotateContinuous", "TranslateContinuous", "Piston", "Linear"
        ])
        
        axis_layout = QHBoxLayout()
        self.spin_x = QDoubleSpinBox(); self.spin_x.setRange(-10, 10); self.spin_x.setSingleStep(0.1); self.spin_x.setValue(0.0); self.spin_x.setPrefix("X: "); self.spin_x.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.spin_y = QDoubleSpinBox(); self.spin_y.setRange(-10, 10); self.spin_y.setSingleStep(0.1); self.spin_y.setValue(1.0); self.spin_y.setPrefix("Y: "); self.spin_y.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.spin_z = QDoubleSpinBox(); self.spin_z.setRange(-10, 10); self.spin_z.setSingleStep(0.1); self.spin_z.setValue(0.0); self.spin_z.setPrefix("Z: "); self.spin_z.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.spin_x.setToolTip("X-axis (Lateral in 3D viewer)")
        self.spin_y.setToolTip("Y-axis (Forward in Unity / Z in 3D viewer)")
        self.spin_z.setToolTip("Z-axis (Vertical in Unity / Y in 3D viewer)")
        axis_layout.addWidget(self.spin_x); axis_layout.addWidget(self.spin_y); axis_layout.addWidget(self.spin_z)
        
        self.spin_speed = QDoubleSpinBox(); self.spin_speed.setRange(-100, 100); self.spin_speed.setValue(1.0); self.spin_speed.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        
        self.check_real_actuator = QCheckBox("Real Physical Actuator")
        self.check_real_actuator.setChecked(False)
        self.edit_real_port = QLineEdit("R0_0")
        self.real_port_container = QWidget()
        real_layout = QFormLayout(self.real_port_container)
        real_layout.setContentsMargins(0, 0, 0, 0)
        real_layout.addRow("Real Port ID:", self.edit_real_port)
        self.real_port_container.setVisible(False)

        def toggle_real_actuator(active):
            self.real_port_container.setVisible(active)
            self.wizard.update_json_preview()

        self.check_real_actuator.toggled.connect(toggle_real_actuator)
        self.edit_real_port.textChanged.connect(lambda: self.wizard.update_json_preview())

        self.layout.addRow("Pin ID:", self.edit_pin)
        self.layout.addRow("Target Node:", self.edit_node)
        self.layout.addRow("Behavior:", self.combo_behavior)
        self.layout.addRow("Stop Sensor:", self.combo_stop_sens)
        self.layout.addRow("Axis (X,Y,Z):", axis_layout)
        self.layout.addRow("Speed:", self.spin_speed)
        self.layout.addRow(self.check_visible)
        self.layout.addRow(self.check_real_actuator)
        self.layout.addRow(self.real_port_container)
        
        btn_del = QPushButton("Delete")
        btn_del.setStyleSheet("background-color: #a93226;")
        btn_del.clicked.connect(lambda: self.wizard.remove_actuator(self))
        self.layout.addRow(btn_del)
        
        self.update_stop_sensors()
        self.spin_x.valueChanged.connect(self.update_visuals)
        self.spin_y.valueChanged.connect(self.update_visuals)
        self.spin_z.valueChanged.connect(self.update_visuals)
        self.spin_speed.valueChanged.connect(self.update_visuals)
        self.edit_node.textChanged.connect(self.update_visuals)
        self.combo_behavior.currentTextChanged.connect(self.update_visuals)
        self.arrow = None; self.update_visuals(); self.validate()

    def validate(self):
        valid = is_valid_aas_id(self.edit_pin.text())
        self.edit_pin.setStyleSheet(VALID_STYLE if valid else ERROR_STYLE)
        self.wizard.update_validation_ui()

    def activate(self):
        self.wizard.current_config_widget = self
        for w in self.wizard.actuator_widgets + self.wizard.sensor_widgets:
            w.setProperty("active", "false")
            if isinstance(w, ActuatorWidget): w.hide_visuals()
            w.style().unpolish(w); w.style().polish(w)
        self.setProperty("active", "true")
        self.style().unpolish(self); self.style().polish(self)
        self.update_visuals()

    def mousePressEvent(self, event):
        self.activate()
        super().mousePressEvent(event)

    def update_stop_sensors(self):
        current = self.combo_stop_sens.currentText()
        self.combo_stop_sens.clear()
        self.combo_stop_sens.addItem("")
        for sw in self.wizard.sensor_widgets:
            pin = sw.edit_pin.text()
            if pin: self.combo_stop_sens.addItem(pin)
        self.combo_stop_sens.setCurrentText(current)

    def set_node(self, name):
        self.edit_node.setPlaceholderText("")
        self.edit_node.setText(name)
        self.update_visuals()

    def hide_visuals(self):
        if self.arrow:
            try:
                self.wizard.plotter.remove_actor(self.arrow)
            except Exception:
                pass
            self.arrow = None
            if hasattr(self.wizard, 'plotter') and self.wizard.plotter:
                self.wizard.plotter.render()

    def update_visuals(self, force=False):
        self.hide_visuals()
        if not force and self.property("active") != "true":
            return
        if not hasattr(self.wizard, 'plotter') or not self.wizard.plotter:
            return

        raw_axis = np.array([self.spin_x.value(), self.spin_y.value(), self.spin_z.value()], dtype=float)
        # Flip direction if speed is negative
        speed_val = self.spin_speed.value()
        effective_axis = -raw_axis if speed_val < 0 else raw_axis

        # Map Unity/AAS coordinate conventions:
        # Input X -> 3D X
        # Input Y -> 3D Z (forward along belt)
        # Input Z -> 3D Y (vertical / up)
        vis_axis = np.array([effective_axis[0], effective_axis[2], effective_axis[1]], dtype=float)

        axis_norm = np.linalg.norm(vis_axis)
        if axis_norm < 1e-6:
            return

        norm_dir = vis_axis / axis_norm

        node = self.edit_node.text().strip()
        all_meshes = []
        if node:
            family = self.wizard.get_node_family(node)
            all_meshes = [self.wizard.meshes[n] for n in family if n in self.wizard.meshes]
            if not all_meshes and node in self.wizard.meshes:
                all_meshes = [self.wizard.meshes[node]]
            if not all_meshes:
                node_lower = node.lower()
                for k, m in self.wizard.meshes.items():
                    if k.lower() == node_lower or node_lower in k.lower() or k.lower() in node_lower:
                        all_meshes.append(m)

        # Fallback to all meshes if node meshes not identified
        if not all_meshes and self.wizard.meshes:
            all_meshes = list(self.wizard.meshes.values())

        if all_meshes:
            b = np.array([m.bounds for m in all_meshes])
            min_b = np.min(b[:, [0, 2, 4]], axis=0) # [xmin, ymin, zmin]
            max_b = np.max(b[:, [1, 3, 5]], axis=0) # [xmax, ymax, zmax]
            center = (min_b + max_b) / 2.0
            extents = np.maximum(max_b - min_b, 1e-4)

            up_axis_idx = 2 if self.wizard.model_type == "URDF" else 1

            dim_along_dir = abs(norm_dir[0]) * extents[0] + abs(norm_dir[1]) * extents[1] + abs(norm_dir[2]) * extents[2]
            max_dim = np.max(extents)

            if dim_along_dir > 0.1 * max_dim:
                arrow_len = dim_along_dir * 0.75
            else:
                arrow_len = max_dim * 0.5
            arrow_len = max(arrow_len, 0.05)

            arrow_center = center.copy()
            tip_rad = max(0.07 * arrow_len, 0.015)
            shaft_rad = max(0.03 * arrow_len, 0.007)

            is_horizontal_motion = abs(norm_dir[up_axis_idx]) < 0.7
            behavior = self.combo_behavior.currentText()
            if is_horizontal_motion and behavior in ["Conveyor", "TranslateContinuous", "Linear", "Piston"]:
                arrow_center[up_axis_idx] = max_b[up_axis_idx] + shaft_rad * 1.5
            else:
                arrow_center[up_axis_idx] = center[up_axis_idx]

            start_point = arrow_center - norm_dir * (arrow_len / 2.0)

            try:
                arrow_mesh = pv.Arrow(
                    start=start_point,
                    direction=norm_dir,
                    tip_length=0.25,
                    tip_radius=tip_rad,
                    shaft_radius=shaft_rad,
                    scale=arrow_len
                )
                self.arrow = self.wizard.plotter.add_mesh(
                    arrow_mesh,
                    color="#FFD700",
                    smooth_shading=True,
                    ambient=0.4,
                    diffuse=0.8,
                    name=f"actuator_arrow_{id(self)}"
                )

                # Match the system transform (position, rotation, scale) of the current model
                M = getattr(self.wizard, 'current_system_matrix', None)
                if M is None and self.wizard.actors:
                    for a in self.wizard.actors.values():
                        if hasattr(a, 'user_matrix') and a.user_matrix is not None:
                            M = a.user_matrix
                            break
                if M is not None and self.arrow:
                    self.arrow.user_matrix = M
                self.wizard.plotter.render()
            except Exception as e:
                print(f"[WIZARD] Error adding arrow: {e}")
                traceback.print_exc()
            
        # Install event filter on ALL child widgets to auto-select/activate this ActuatorWidget on click or focus
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            # Ignore wheel events on focus widgets to allow outer scroll area to scroll
            event.ignore()
            return True
        if event.type() in [QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn]:
            self.activate()
        return super().eventFilter(watched, event)

    def get_data(self):
        return {
            "pin_id": self.edit_pin.text(),
            "node": self.edit_node.text(),
            "behavior": self.combo_behavior.currentText(),
            "stop_sensor": self.combo_stop_sens.currentText(),
            "axis": [self.spin_x.value(), self.spin_y.value(), self.spin_z.value()],
            "speed": self.spin_speed.value(),
            "visible": self.check_visible.isChecked(),
            "real_actuator_enabled": self.check_real_actuator.isChecked(),
            "real_port_id": self.edit_real_port.text().strip()
        }

class SensorWidget(QGroupBox):
    def __init__(self, parent_wizard):
        super().__init__("Sensor Configuration")
        self.wizard = parent_wizard
        layout = QFormLayout(self); layout.setSpacing(5); layout.setContentsMargins(10, 15, 10, 10)
        
        self.edit_pin = QLineEdit("OUT_Sensor")
        self.edit_pin.textChanged.connect(self.on_pin_changed)
        
        self.edit_node = QLineEdit("")
        self.edit_node.setReadOnly(True)
        self.edit_node.setPlaceholderText("Select node in Tree or 3D...")
        
        self.check_visible = QCheckBox("Visible Trigger Zone")
        self.check_visible.setChecked(False)
        self.check_active_low = QCheckBox("Active Low")
        self.check_snap = QCheckBox("Snap Product to Sensor Center")
        self.check_snap.setChecked(False)
        self.check_snap.toggled.connect(lambda: self.wizard.update_json_preview())
        
        self.check_real_sensor = QCheckBox("Real Physical Sensor")
        self.check_real_sensor.setChecked(False)
        self.edit_real_port = QLineEdit("I0_0")
        self.edit_real_port.setPlaceholderText("Physical Port ID (e.g. I0_0)")
        self.edit_real_port.setEnabled(False)
        
        # Container to hide/show Real Port ID field dynamically
        self.real_port_container = QWidget()
        real_port_layout = QFormLayout(self.real_port_container)
        real_port_layout.setContentsMargins(0, 0, 0, 0)
        real_port_layout.addRow("Real Port ID:", self.edit_real_port)
        self.real_port_container.setVisible(False)

        def toggle_real_sensor(active):
            self.edit_real_port.setEnabled(active)
            self.real_port_container.setVisible(active)
            self.wizard.update_json_preview()

        self.check_real_sensor.toggled.connect(toggle_real_sensor)
        self.edit_real_port.textChanged.connect(lambda: self.wizard.update_json_preview())

        # --- Move-On-State UI fields ---
        self.check_move = QCheckBox("Move Target on State")
        self.check_move.setChecked(False)
        self.edit_move_target = QLineEdit("")
        self.edit_move_target.setReadOnly(True)
        self.edit_move_target.setPlaceholderText("Select component in Tree or 3D...")
        self.combo_move_state = QComboBox()
        self.combo_move_state.addItems(["HIGH", "LOW"])
        self.spin_mpos_x = QDoubleSpinBox(); self.spin_mpos_x.setRange(-999, 999); self.spin_mpos_x.setSingleStep(0.1)
        self.spin_mpos_y = QDoubleSpinBox(); self.spin_mpos_y.setRange(-999, 999); self.spin_mpos_y.setSingleStep(0.1)
        self.spin_mpos_z = QDoubleSpinBox(); self.spin_mpos_z.setRange(-999, 999); self.spin_mpos_z.setSingleStep(0.1)
        self.spin_mrot_x = QDoubleSpinBox(); self.spin_mrot_x.setRange(-360, 360); self.spin_mrot_x.setSingleStep(5)
        self.spin_mrot_y = QDoubleSpinBox(); self.spin_mrot_y.setRange(-360, 360); self.spin_mrot_y.setSingleStep(5)
        self.spin_mrot_z = QDoubleSpinBox(); self.spin_mrot_z.setRange(-360, 360); self.spin_mrot_z.setSingleStep(5)
        self.spin_mdur = QDoubleSpinBox(); self.spin_mdur.setRange(0, 60); self.spin_mdur.setValue(0.5); self.spin_mdur.setSingleStep(0.1)

        self.combo_move_mode = QComboBox()
        self.combo_move_mode.addItems(["Position and Rotation", "Position Only", "Rotation Only"])
        
        self.edit_move_target.setEnabled(False)
        self.combo_move_state.setEnabled(False)
        self.combo_move_mode.setEnabled(False)
        self.spin_mpos_x.setEnabled(False); self.spin_mpos_y.setEnabled(False); self.spin_mpos_z.setEnabled(False)
        self.spin_mrot_x.setEnabled(False); self.spin_mrot_y.setEnabled(False); self.spin_mrot_z.setEnabled(False)
        self.spin_mdur.setEnabled(False)

        self.check_move.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.edit_move_target.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_move_state.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.combo_move_mode.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_mpos_x.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_mpos_y.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_mpos_z.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_mrot_x.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_mrot_y.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_mrot_z.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.spin_mdur.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Event filter to safely detect clicks/focus on the line edits without causing recursions
        self.edit_node.installEventFilter(self)
        self.edit_move_target.installEventFilter(self)

        # Helper to toggle visibility of a form row (label widget + field widget/layout)
        def set_row_visible(form_layout, row_idx, visible):
            label_item = form_layout.itemAt(row_idx, QFormLayout.ItemRole.LabelRole)
            field_item = form_layout.itemAt(row_idx, QFormLayout.ItemRole.FieldRole)
            if label_item and label_item.widget():
                label_item.widget().setVisible(visible)
            if field_item:
                if field_item.widget():
                    field_item.widget().setVisible(visible)
                elif field_item.layout():
                    for i in range(field_item.layout().count()):
                        w = field_item.layout().itemAt(i).widget()
                        if w: w.setVisible(visible)

        # Helpers to wrap layouts in dummy widgets for perfect visibility toggling
        pos_layout = QHBoxLayout()
        pos_layout.setContentsMargins(0, 0, 0, 0)
        pos_layout.addWidget(QLabel("X:")); pos_layout.addWidget(self.spin_mpos_x)
        pos_layout.addWidget(QLabel("Y:")); pos_layout.addWidget(self.spin_mpos_y)
        pos_layout.addWidget(QLabel("Z:")); pos_layout.addWidget(self.spin_mpos_z)

        rot_layout = QHBoxLayout()
        rot_layout.setContentsMargins(0, 0, 0, 0)
        rot_layout.addWidget(QLabel("X:")); rot_layout.addWidget(self.spin_mrot_x)
        rot_layout.addWidget(QLabel("Y:")); rot_layout.addWidget(self.spin_mrot_y)
        rot_layout.addWidget(QLabel("Z:")); rot_layout.addWidget(self.spin_mrot_z)

        self.pos_container = QWidget(); self.pos_container.setLayout(pos_layout)
        self.rot_container = QWidget(); self.rot_container.setLayout(rot_layout)

        # Connect Move Mode combobox
        def update_move_mode_visibility():
            mode = self.combo_move_mode.currentText()
            show_pos = mode in ["Position and Rotation", "Position Only"]
            show_rot = mode in ["Position and Rotation", "Rotation Only"]
            
            self.pos_container.setVisible(show_pos)
            self.spin_mpos_x.setEnabled(show_pos); self.spin_mpos_y.setEnabled(show_pos); self.spin_mpos_z.setEnabled(show_pos)
            
            self.rot_container.setVisible(show_rot)
            self.spin_mrot_x.setEnabled(show_rot); self.spin_mrot_y.setEnabled(show_rot); self.spin_mrot_z.setEnabled(show_rot)
            
            # Hide/show the labels for position and rotation rows
            pos_label = layout.labelForField(self.pos_container)
            if pos_label: pos_label.setVisible(show_pos)
            rot_label = layout.labelForField(self.rot_container)
            if rot_label: rot_label.setVisible(show_rot)
            
            self.wizard.update_json_preview()

        self.combo_move_mode.currentTextChanged.connect(lambda: update_move_mode_visibility())

        def toggle_move_fields(active):
            # Show/hide all move-related rows in the main layout
            for row_w in [self.edit_move_target, self.combo_move_state, self.combo_move_mode, self.spin_mdur, self.pos_container, self.rot_container]:
                lbl = layout.labelForField(row_w)
                if lbl: lbl.setVisible(active)
                row_w.setVisible(active)
                
            self.edit_move_target.setEnabled(active)
            self.combo_move_state.setEnabled(active)
            self.combo_move_mode.setEnabled(active)
            self.spin_mdur.setEnabled(active)
            
            if active:
                self.active_selection_field = "move"
                self.edit_move_target.setStyleSheet("border: 2px solid #2874a6;")
                self.edit_node.setStyleSheet("")
                update_move_mode_visibility()
            else:
                self.active_selection_field = "node"
                self.edit_node.setStyleSheet("border: 2px solid #2874a6;")
                self.edit_move_target.setStyleSheet("")
            self.wizard.update_json_preview()
            
        self.check_move.toggled.connect(toggle_move_fields)
        self.edit_move_target.textChanged.connect(lambda: self.wizard.update_json_preview())
        self.combo_move_state.currentTextChanged.connect(lambda: self.wizard.update_json_preview())
        self.spin_mpos_x.valueChanged.connect(lambda: self.wizard.update_json_preview())
        self.spin_mpos_y.valueChanged.connect(lambda: self.wizard.update_json_preview())
        self.spin_mpos_z.valueChanged.connect(lambda: self.wizard.update_json_preview())
        self.spin_mrot_x.valueChanged.connect(lambda: self.wizard.update_json_preview())
        self.spin_mrot_y.valueChanged.connect(lambda: self.wizard.update_json_preview())
        self.spin_mrot_z.valueChanged.connect(lambda: self.wizard.update_json_preview())
        self.spin_mdur.valueChanged.connect(lambda: self.wizard.update_json_preview())

        self.combo_behavior = QComboBox(); self.combo_behavior.addItems(["TriggerZone", "Presence", "Proximity"])
        self.edit_filter = QLineEdit("Metal")
        
        layout.addRow("Pin ID:", self.edit_pin)
        layout.addRow("Detection Node:", self.edit_node)
        layout.addRow("Behavior:", self.combo_behavior)
        layout.addRow("Target Filter:", self.edit_filter)
        layout.addRow(self.check_visible)
        layout.addRow(self.check_active_low)
        layout.addRow(self.check_snap)
        layout.addRow(self.check_real_sensor)
        layout.addRow(self.real_port_container)
        
        layout.addRow(self.check_move)
        layout.addRow("Move Target:", self.edit_move_target)
        layout.addRow("Move on State:", self.combo_move_state)
        layout.addRow("Move Mode:", self.combo_move_mode)
        layout.addRow("Move Local Pos:", self.pos_container)
        layout.addRow("Move Local Rot:", self.rot_container)
        layout.addRow("Move Duration (s):", self.spin_mdur)

        # Start hidden by default
        for row_w in [self.edit_move_target, self.combo_move_state, self.combo_move_mode, self.spin_mdur, self.pos_container, self.rot_container]:
            lbl = layout.labelForField(row_w)
            if lbl: lbl.setVisible(False)
            row_w.setVisible(False)
        
        btn_del = QPushButton("Delete")
        btn_del.setStyleSheet("background-color: #a93226;")
        btn_del.clicked.connect(lambda: self.wizard.remove_sensor(self))
        layout.addRow(btn_del)
        self.validate()

        # Install event filter on ALL child widgets to auto-select/activate this SensorWidget on click or focus
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            # Ignore wheel events on inputs to allow outer scroll area to scroll
            event.ignore()
            return True
            
        # Automatically activate this widget if any of its children get clicked or focused
        if event.type() in [QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn]:
            self.activate()

        # Handle text box selection routing
        if event.type() == QEvent.Type.MouseButtonPress:
            if watched == self.edit_node:
                self.active_selection_field = "node"
                self.edit_node.setStyleSheet("border: 2px solid #2874a6;")
                self.edit_move_target.setStyleSheet("")
            elif watched == self.edit_move_target and self.edit_move_target.isEnabled():
                self.active_selection_field = "move"
                self.edit_move_target.setStyleSheet("border: 2px solid #2874a6;")
                self.edit_node.setStyleSheet("")
        return super().eventFilter(watched, event)

    def on_pin_changed(self):
        self.validate()
        self.wizard.update_all_actuator_dropdowns()

    def validate(self):
        valid = is_valid_aas_id(self.edit_pin.text())
        self.edit_pin.setStyleSheet(VALID_STYLE if valid else ERROR_STYLE)
        self.wizard.update_validation_ui()

    def activate(self):
        self.wizard.current_config_widget = self
        for w in self.wizard.actuator_widgets + self.wizard.sensor_widgets:
            w.setProperty("active", "false")
            if isinstance(w, ActuatorWidget): w.hide_visuals()
            w.style().unpolish(w); w.style().polish(w)
        self.setProperty("active", "true")
        self.style().unpolish(self); self.style().polish(self)

    def mousePressEvent(self, event):
        self.activate()
        child = self.childAt(event.position().toPoint())
        if child:
            child.setFocus()
        super().mousePressEvent(event)

    def set_node(self, name):
        if hasattr(self, "active_selection_field") and self.active_selection_field == "move":
            self.edit_move_target.setPlaceholderText("")
            self.edit_move_target.setText(name)
        else:
            self.edit_node.setPlaceholderText("")
            self.edit_node.setText(name)

    def get_data(self):
        data = { 
            "pin_id": self.edit_pin.text(), 
            "node": self.edit_node.text(), 
            "behavior": self.combo_behavior.currentText(), 
            "target_filter": self.edit_filter.text(), 
            "visible": self.check_visible.isChecked(), 
            "active_low": self.check_active_low.isChecked(),
            "snap_product": self.check_snap.isChecked(),
            "real_sensor_enabled": self.check_real_sensor.isChecked(),
            "real_port_id": self.edit_real_port.text().strip()
        }
        if self.check_move.isChecked():
            data["move_target"] = self.edit_move_target.text().strip()
            data["move_on_state"] = self.combo_move_state.currentText() == "HIGH"
            # Map clean strings for backend serialization
            mode_map = {"Position and Rotation": "Both", "Position Only": "Position", "Rotation Only": "Rotation"}
            data["move_mode"] = mode_map.get(self.combo_move_mode.currentText(), "Both")
            data["move_position"] = [self.spin_mpos_x.value(), self.spin_mpos_y.value(), self.spin_mpos_z.value()]
            data["move_rotation"] = [self.spin_mrot_x.value(), self.spin_mrot_y.value(), self.spin_mrot_z.value()]
            data["move_duration"] = self.spin_mdur.value()
        return data

class TriggerRow(QWidget):
    def __init__(self, parent_wizard, sensor_list, initial_sensor="", initial_value=True, on_delete=None, on_changed=None, initial_kit=None):
        super().__init__()
        self.on_changed = on_changed
        self.parent_wizard = parent_wizard
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(5)
        
        self.combo_sensor = QComboBox()
        self.combo_sensor.addItems(sensor_list)
        
        default_kit = ""
        if hasattr(parent_wizard, "edit_kit_name"):
            default_kit = parent_wizard.edit_kit_name.text()
        elif hasattr(parent_wizard, "get_current_kit"):
            default_kit = parent_wizard.get_current_kit()
            
        kit = initial_kit or default_kit
        target_item = f"{kit} - {initial_sensor}" if kit and initial_sensor else initial_sensor
        
        if target_item in sensor_list:
            self.combo_sensor.setCurrentText(target_item)
        elif initial_sensor in sensor_list:
            self.combo_sensor.setCurrentText(initial_sensor)
        else:
            matched = False
            for item in sensor_list:
                if item.endswith(f" - {initial_sensor}"):
                    self.combo_sensor.setCurrentText(item)
                    matched = True
                    break
            if not matched and sensor_list:
                self.combo_sensor.setCurrentIndex(0)
        
        self.combo_value = QComboBox()
        self.combo_value.addItems(["true", "false"])
        self.combo_value.setCurrentText("true" if initial_value else "false")
        
        btn_del = QPushButton("❌")
        btn_del.setFixedWidth(30)
        btn_del.setStyleSheet("background-color: #a93226; padding: 2px;")
        if on_delete:
            btn_del.clicked.connect(on_delete)
            
        layout.addWidget(self.combo_sensor, 2)
        layout.addWidget(self.combo_value, 1)
        layout.addWidget(btn_del)
        
        self.combo_sensor.currentTextChanged.connect(self._trigger_change)
        self.combo_value.currentTextChanged.connect(self._trigger_change)
        
    def _trigger_change(self):
        if self.on_changed:
            self.on_changed()
            
    def get_data(self):
        text = self.combo_sensor.currentText()
        if text.startswith("[Rule] "):
            seq_name = text[len("[Rule] "):].strip()
            return {
                "sequence": seq_name,
                "value": self.combo_value.currentText() == "true"
            }
            
        default_kit = ""
        if hasattr(self.parent_wizard, "edit_kit_name"):
            default_kit = self.parent_wizard.edit_kit_name.text()
        elif hasattr(self.parent_wizard, "get_current_kit"):
            default_kit = self.parent_wizard.get_current_kit()
            
        if " - " in text:
            parts = text.split(" - ", 1)
            kit = parts[0].strip()
            sensor = parts[1].strip()
        else:
            kit = default_kit
            sensor = text.strip()
            
        return {
            "kit": kit,
            "sensor": sensor,
            "value": self.combo_value.currentText() == "true"
        }

class ConveyorWizard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Conveyor Wizard")
        self.config_data = {"kit_name": "", "aas_id": "", "description": "", "components": []}
        self.logic_rules = {"start": [], "sequences": []}
        self.glb_path = None; self.meshes = {}; self.actors = {}; self.layout_actor_names = []; self.actuator_widgets = []; self.sensor_widgets = []; 
        self.current_config_widget = None
        self.current_system_matrix = None
        self.model_type = "GLB"
        self.robot = None
        self.current_urdf_xml = ""
        self.current_urdf_path = ""
        self.current_urdf_tree = None
        self.files_to_upload = {}
        self.current_robot_root = ""
        self.apply_styles(); self.setup_ui()
        self.showMaximized()


        
    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; }
            QWidget { background-color: #1e1e1e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }
            QLabel { font-size: 13px; font-weight: 500; }
            QLineEdit, QDoubleSpinBox, QComboBox, QTextEdit { 
                background-color: #2d2d2d; border: 1px solid #3d3d3d; border-radius: 4px; padding: 6px; color: white; 
            }
            QPushButton { background-color: #007acc; color: white; border: none; border-radius: 4px; padding: 8px 16px; font-weight: bold; }
            QPushButton:hover { background-color: #008be5; }
            QGroupBox { border: 1px solid #3d3d3d; border-radius: 8px; margin-top: 15px; padding-top: 20px; font-weight: bold; background-color: #252526; }
            QGroupBox[active="true"] { border: 2px solid #007acc; background-color: #2d2d30; }
            QTreeWidget { background-color: #252526; border: 1px solid #3d3d3d; border-radius: 4px; }
            QTreeWidget::item:selected { background-color: #094771; }
            QSplitter::handle { background-color: #333; }
            QScrollArea { border: none; background-color: transparent; }
        """)

    def setup_ui(self):
        central = QWidget(); self.setCentralWidget(central); main_layout = QVBoxLayout(central); main_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        
        self.wizard_panel = QWidget(); self.wizard_panel.setMinimumWidth(400); self.wizard_layout = QVBoxLayout(self.wizard_panel)
        self.step_label = QLabel("Step 1 of 6: Model Selection"); self.step_label.setStyleSheet("font-size: 20px; color: #007acc; margin-bottom: 5px;")
        self.wizard_layout.addWidget(self.step_label); self.stack = QStackedWidget(); self.wizard_layout.addWidget(self.stack); self.setup_steps()
        
        self.lbl_global_error = QLabel(VALIDATION_MSG); self.lbl_global_error.setStyleSheet("color: #e74c3c; font-weight: bold; background-color: #2d1e1e; padding: 10px; border-radius: 4px;")
        self.lbl_global_error.setVisible(False); self.wizard_layout.addWidget(self.lbl_global_error)

        nav_layout = QHBoxLayout(); self.btn_prev = QPushButton("← Back"); self.btn_prev.setMinimumHeight(40); self.btn_prev.clicked.connect(self.prev_step); self.btn_prev.setEnabled(False)
        self.btn_next = QPushButton("Next →"); self.btn_next.setMinimumHeight(40); self.btn_next.clicked.connect(self.next_step)
        nav_layout.addWidget(self.btn_prev); nav_layout.addWidget(self.btn_next); self.wizard_layout.addLayout(nav_layout)
        
        self.viewer_container = QWidget(); viewer_layout = QVBoxLayout(self.viewer_container); viewer_layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = BackgroundPlotter(show=False); self.plotter.set_background("#161616"); self.plotter.add_axes()
        
        self.plotter.enable_mesh_picking(callback=self.on_mesh_picked, show=False, show_message=False, left_clicking=True)
        self.cleanup_timer = QTimer(); self.cleanup_timer.timeout.connect(self.kill_overlays); self.cleanup_timer.start(200)
        
        viewer_layout.addWidget(self.plotter.interactor)
        try: self.plotter.camera.up = (0, 1, 0); self.plotter.enable_terrain_style()
        except: pass

        self.node_sidebar = QGroupBox("Model Structure"); self.node_sidebar.setMinimumWidth(220); side_vbox = QVBoxLayout(self.node_sidebar)
        self.node_tree = QTreeWidget(); self.node_tree.setHeaderLabel("Hierarchy"); self.node_tree.itemClicked.connect(self.on_tree_item_clicked); self.node_tree.mousePressEvent = self.tree_mouse_press; side_vbox.addWidget(self.node_tree)

        self.splitter.addWidget(self.wizard_panel); self.splitter.addWidget(self.viewer_container); self.splitter.addWidget(self.node_sidebar)
        self.splitter.setStretchFactor(1, 4); main_layout.addWidget(self.splitter)

    def kill_overlays(self):
        try:
            self.plotter.disable_picking_help()
            for actor_name in list(self.plotter.renderer.actors.keys()):
                if any(x in actor_name.lower() for x in ["text", "message", "pick", "prompt", "info"]): self.plotter.remove_actor(actor_name)
        except: pass

    def tree_mouse_press(self, event):
        item = self.node_tree.itemAt(event.pos())
        if not item: self.node_tree.clearSelection(); self.deselect_all()
        QTreeWidget.mousePressEvent(self.node_tree, event)

    def deselect_all(self):
        for actor in self.actors.values(): actor.prop.opacity = 1.0

    def open_program_rules_dialog(self):
        dlg = AASRulesEditorDialog(self)
        dlg.exec()

    def setup_steps(self):
        # P1: Model Selection
        self.page1 = QWidget(); l1 = QVBoxLayout(self.page1); l1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        il = QLabel("📁"); il.setAlignment(Qt.AlignmentFlag.AlignCenter); il.setStyleSheet("font-size: 60px;")
        
        bl = QVBoxLayout(); bl.setSpacing(10); bl.setContentsMargins(50, 0, 50, 0)
        self.btn_select_glb = QPushButton("Select GLB File"); self.btn_select_glb.setMinimumHeight(55); self.btn_select_glb.clicked.connect(self.browse_glb)
        self.btn_select_urdf = QPushButton("Select URDF / Xacro Folder")
        self.btn_select_urdf.setMinimumHeight(55)
        self.btn_select_urdf.setStyleSheet("background-color: #2980b9;")
        self.btn_select_urdf.clicked.connect(self.browse_urdf)
        self.btn_manage_aas = QPushButton("Manage Deployed AAS"); self.btn_manage_aas.setMinimumHeight(55); self.btn_manage_aas.setStyleSheet("background-color: #27ae60;")
        self.btn_manage_aas.clicked.connect(self.manage_deployed_aas)
        self.btn_preview_layout = QPushButton("Preview Deployed Layout"); self.btn_preview_layout.setMinimumHeight(55); self.btn_preview_layout.setStyleSheet("background-color: #8e44ad;")
        self.btn_preview_layout.clicked.connect(self.preview_deployed_layout)
        
        self.btn_program_rules = QPushButton("Program Deployed AAS Rules")
        self.btn_program_rules.setMinimumHeight(55)
        self.btn_program_rules.setStyleSheet("background-color: #d35400; color: white; font-weight: bold;")
        self.btn_program_rules.clicked.connect(self.open_program_rules_dialog)
        
        bl.addWidget(self.btn_select_glb); bl.addWidget(self.btn_select_urdf); bl.addWidget(self.btn_manage_aas); bl.addWidget(self.btn_preview_layout); bl.addWidget(self.btn_program_rules)
        
        self.lbl_glb_path = QLabel("No file selected")
        l1.addWidget(il); l1.addLayout(bl); l1.addWidget(self.lbl_glb_path); self.stack.addWidget(self.page1)

        # P2: Basic & Transform
        self.page2 = QWidget(); l2 = QVBoxLayout(self.page2); info_group = QGroupBox("System Info"); ifl = QFormLayout(info_group)
        self.edit_kit_name = QLineEdit("ConveyorKit"); self.edit_aas_id = QLineEdit("Conveyor_01"); self.edit_desc = QLineEdit("Standard Conveyor System")
        self.edit_aas_id.textChanged.connect(self.update_validation_ui)
        self.check_is_dynamic = QCheckBox("Is Dynamic (adds Rigidbody/Physics in Unity)")
        
        ifl.addRow("Kit Name:", self.edit_kit_name); ifl.addRow("AAS ID:", self.edit_aas_id); ifl.addRow("Description:", self.edit_desc)
        ifl.addRow("", self.check_is_dynamic)

        comm_group = QGroupBox("Communication Settings")
        cfl = QFormLayout(comm_group)
        self.combo_comm_protocol = QComboBox()
        self.combo_comm_protocol.addItems(["None", "Sockets"])
        self.edit_comm_ip = QLineEdit("192.168.10.1")
        self.spin_comm_port = QSpinBox()
        self.spin_comm_port.setRange(1, 65535)
        self.spin_comm_port.setValue(8888)

        lbl_ip = QLabel("IP Address:")
        lbl_port = QLabel("Port:")

        def _on_comm_proto_changed(proto):
            is_sockets = (proto == "Sockets")
            lbl_ip.setVisible(is_sockets)
            self.edit_comm_ip.setVisible(is_sockets)
            lbl_port.setVisible(is_sockets)
            self.spin_comm_port.setVisible(is_sockets)

        self.combo_comm_protocol.currentTextChanged.connect(_on_comm_proto_changed)
        _on_comm_proto_changed(self.combo_comm_protocol.currentText())

        cfl.addRow("Protocol:", self.combo_comm_protocol)
        cfl.addRow(lbl_ip, self.edit_comm_ip)
        cfl.addRow(lbl_port, self.spin_comm_port)
        
        trans_group = QGroupBox("System Transform"); tfl = QFormLayout(trans_group)
        self.sys_pos = [QDoubleSpinBox() for _ in range(3)]; self.sys_rot = [QDoubleSpinBox() for _ in range(3)]; self.sys_scale = [QDoubleSpinBox() for _ in range(3)]
        pos_l = QHBoxLayout(); rot_l = QHBoxLayout(); sca_l = QHBoxLayout()
        for i in range(3): 
            self.sys_pos[i].setRange(-100, 100); self.sys_pos[i].setSingleStep(0.1); self.sys_pos[i].setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons); pos_l.addWidget(self.sys_pos[i])
            self.sys_pos[i].valueChanged.connect(self.update_system_transform)
            
            self.sys_rot[i].setRange(-360, 360); self.sys_rot[i].setSingleStep(5.0); self.sys_rot[i].setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons); rot_l.addWidget(self.sys_rot[i])
            self.sys_rot[i].valueChanged.connect(self.update_system_transform)
            
            self.sys_scale[i].setRange(0, 100); self.sys_scale[i].setValue(1.0); self.sys_scale[i].setSingleStep(0.1); self.sys_scale[i].setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons); sca_l.addWidget(self.sys_scale[i])
            self.sys_scale[i].valueChanged.connect(self.update_system_transform)
            
        tfl.addRow("Position:", pos_l)
        tfl.addRow("Rotation:", rot_l)
        tfl.addRow("Scale:", sca_l)
        
        self.check_preview_layout = QCheckBox("View in Current Project (Preview Factory Layout)")
        self.check_preview_layout.setStyleSheet("font-weight: bold; color: #3498db;")
        self.check_preview_layout.stateChanged.connect(self.toggle_layout_preview)
        tfl.addRow("", self.check_preview_layout)
        
        l2.addWidget(info_group); l2.addWidget(trans_group); l2.addWidget(comm_group); l2.addStretch(); self.stack.addWidget(self.page2)
        
        # P3: Sensors
        self.page3 = QWidget(); l3 = QVBoxLayout(self.page3); self.scroll_sens = QScrollArea(); self.scroll_sens.setWidgetResizable(True); self.sens_container = QWidget(); self.sens_layout = QVBoxLayout(self.sens_container); self.scroll_sens.setWidget(self.sens_container)
        l3.addWidget(self.scroll_sens); bs = QPushButton("➕ Add Sensor"); bs.clicked.connect(self.add_sensor); l3.addWidget(bs); self.stack.addWidget(self.page3)

        # P4: Actuators
        self.page4 = QWidget(); l4 = QVBoxLayout(self.page4); self.scroll_act = QScrollArea(); self.scroll_act.setWidgetResizable(True); self.act_container = QWidget(); self.act_layout = QVBoxLayout(self.act_container); self.scroll_act.setWidget(self.act_container)
        l4.addWidget(self.scroll_act); ba = QPushButton("➕ Add Actuator"); ba.clicked.connect(self.add_actuator); l4.addWidget(ba); self.stack.addWidget(self.page4)
        
        # P5: Preview (former P6)
        self.page5 = QWidget()
        l5 = QVBoxLayout(self.page5)
        self.json_preview = QTextEdit()
        self.json_preview.setReadOnly(True)
        l5.addWidget(self.json_preview)
        self.stack.addWidget(self.page5)

    def setup_control_logic_page(self):
        l5 = QVBoxLayout(self.page5)
        l5.setContentsMargins(10, 10, 10, 10)
        l5.setSpacing(10)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        l5.addWidget(splitter)
        
        # Panel A: Left (Sequences List)
        panel_a = QGroupBox("Sequences")
        layout_a = QVBoxLayout(panel_a)
        layout_a.setContentsMargins(10, 15, 10, 10)
        self.list_sequences = QListWidget()
        self.list_sequences.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_sequences.model().rowsMoved.connect(self.on_sequences_reordered)
        self.list_sequences.addItem("Start Sequence")
        layout_a.addWidget(self.list_sequences)
        
        btn_layout_a = QHBoxLayout()
        self.btn_add_seq = QPushButton("Add Sequence")
        self.btn_del_seq = QPushButton("Delete Sequence")
        self.btn_del_seq.setStyleSheet("background-color: #a93226;")
        btn_layout_a.addWidget(self.btn_add_seq)
        btn_layout_a.addWidget(self.btn_del_seq)
        layout_a.addLayout(btn_layout_a)
        
        # Panel B: Middle (Trigger Conditions)
        self.group_triggers = QGroupBox("Trigger Conditions")
        layout_b = QVBoxLayout(self.group_triggers)
        layout_b.setContentsMargins(10, 15, 10, 10)
        
        op_layout = QHBoxLayout()
        op_layout.addWidget(QLabel("Operator:"))
        self.combo_trigger_op = QComboBox()
        self.combo_trigger_op.addItems(["AND", "OR"])
        op_layout.addWidget(self.combo_trigger_op)
        op_layout.addStretch()
        layout_b.addLayout(op_layout)
        
        self.scroll_triggers = QScrollArea()
        self.scroll_triggers.setWidgetResizable(True)
        self.triggers_container = QWidget()
        self.triggers_layout = QVBoxLayout(self.triggers_container)
        self.triggers_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.triggers_layout.setContentsMargins(0, 0, 0, 0)
        self.triggers_layout.setSpacing(5)
        self.scroll_triggers.setWidget(self.triggers_container)
        layout_b.addWidget(self.scroll_triggers)
        
        self.btn_add_trigger = QPushButton("➕ Add Trigger Condition")
        layout_b.addWidget(self.btn_add_trigger)
        
        # Panel C: Right (Sequence Steps)
        self.group_steps = QGroupBox("Sequence Steps")
        layout_c = QVBoxLayout(self.group_steps)
        layout_c.setContentsMargins(10, 15, 10, 10)
        
        self.list_steps = QListWidget()
        layout_c.addWidget(self.list_steps)
        
        btn_add_step_layout = QHBoxLayout()
        self.btn_add_set_act = QPushButton("Add Set Actuator")
        self.btn_add_wait_until = QPushButton("Add Wait Until")
        self.btn_add_wait_time = QPushButton("Add Wait Time")
        btn_add_step_layout.addWidget(self.btn_add_set_act)
        btn_add_step_layout.addWidget(self.btn_add_wait_until)
        btn_add_step_layout.addWidget(self.btn_add_wait_time)
        layout_c.addLayout(btn_add_step_layout)
        
        btn_control_layout = QHBoxLayout()
        self.btn_move_up = QPushButton("Move Up")
        self.btn_move_down = QPushButton("Move Down")
        self.btn_delete_step = QPushButton("Delete Step")
        self.btn_move_up.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_move_down.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_delete_step.setStyleSheet("background-color: #a93226;")
        self.btn_delete_step.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_control_layout.addWidget(self.btn_move_up)
        btn_control_layout.addWidget(self.btn_move_down)
        btn_control_layout.addWidget(self.btn_delete_step)
        layout_c.addLayout(btn_control_layout)
        
        # Bottom Edit Pane
        self.group_step_edit = QGroupBox("Edit Selected Step")
        self.step_edit_layout = QVBoxLayout(self.group_step_edit)
        self.step_edit_layout.setContentsMargins(10, 15, 10, 10)
        self.step_edit_stack = QStackedWidget()
        self.step_edit_layout.addWidget(self.step_edit_stack)
        layout_c.addWidget(self.group_step_edit)
        
        # Add to splitter
        splitter.addWidget(panel_a)
        splitter.addWidget(self.group_triggers)
        splitter.addWidget(self.group_steps)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 2)
        
        # Step Edit Stack Pages
        # Page 0: Empty
        page_empty = QWidget()
        layout_empty = QVBoxLayout(page_empty)
        lbl_empty = QLabel("Select a step to edit properties")
        lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout_empty.addWidget(lbl_empty)
        self.step_edit_stack.addWidget(page_empty)
        
        # Page 1: set_actuator
        page_actuator = QWidget()
        layout_actuator = QFormLayout(page_actuator)
        layout_actuator.setContentsMargins(0, 0, 0, 0)
        layout_actuator.setSpacing(5)
        self.edit_step_actuator_combo = QComboBox()
        self.edit_step_actuator_val = QCheckBox("Active / On")
        layout_actuator.addRow("Actuator:", self.edit_step_actuator_combo)
        layout_actuator.addRow("Target State:", self.edit_step_actuator_val)
        self.step_edit_stack.addWidget(page_actuator)
        
        # Page 2: wait_time
        page_time = QWidget()
        layout_time = QFormLayout(page_time)
        layout_time.setContentsMargins(0, 0, 0, 0)
        layout_time.setSpacing(5)
        self.edit_step_wait_time_spin = QDoubleSpinBox()
        self.edit_step_wait_time_spin.setRange(0.0, 3600.0)
        self.edit_step_wait_time_spin.setSingleStep(0.1)
        self.edit_step_wait_time_spin.setSuffix(" seconds")
        self.edit_step_wait_time_spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        layout_time.addRow("Delay:", self.edit_step_wait_time_spin)
        self.step_edit_stack.addWidget(page_time)
        
        # Page 3: wait_until
        page_until = QWidget()
        layout_until = QVBoxLayout(page_until)
        layout_until.setContentsMargins(0, 0, 0, 0)
        layout_until.setSpacing(5)
        
        until_op_layout = QHBoxLayout()
        until_op_layout.addWidget(QLabel("Operator:"))
        self.edit_step_wait_until_op = QComboBox()
        self.edit_step_wait_until_op.addItems(["AND", "OR"])
        until_op_layout.addWidget(self.edit_step_wait_until_op)
        until_op_layout.addStretch()
        layout_until.addLayout(until_op_layout)
        
        self.scroll_step_wait_until = QScrollArea()
        self.scroll_step_wait_until.setWidgetResizable(True)
        self.scroll_step_wait_until.setMinimumHeight(100)
        self.step_wait_until_container = QWidget()
        self.step_wait_until_layout = QVBoxLayout(self.step_wait_until_container)
        self.step_wait_until_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.step_wait_until_layout.setContentsMargins(0, 0, 0, 0)
        self.step_wait_until_layout.setSpacing(5)
        self.scroll_step_wait_until.setWidget(self.step_wait_until_container)
        layout_until.addWidget(self.scroll_step_wait_until)
        
        self.btn_add_step_wait_until_cond = QPushButton("➕ Add Sensor Condition")
        layout_until.addWidget(self.btn_add_step_wait_until_cond)
        self.step_edit_stack.addWidget(page_until)
        
        # Connections
        self.list_sequences.currentRowChanged.connect(self.on_sequence_selected)
        self.btn_add_seq.clicked.connect(self.add_sequence)
        self.btn_del_seq.clicked.connect(self.delete_sequence)
        
        self.combo_trigger_op.currentTextChanged.connect(self.save_trigger_changes)
        self.btn_add_trigger.clicked.connect(self.add_trigger_condition)
        
        self.list_steps.currentRowChanged.connect(self.update_step_edit_pane)
        self.btn_add_set_act.clicked.connect(self.add_set_actuator_step)
        self.btn_add_wait_time.clicked.connect(self.add_wait_time_step)
        self.btn_add_wait_until.clicked.connect(self.add_wait_until_step)
        
        self.btn_move_up.clicked.connect(self.move_step_up)
        self.btn_move_down.clicked.connect(self.move_step_down)
        self.btn_delete_step.clicked.connect(self.delete_step)
        
        self.edit_step_actuator_combo.currentTextChanged.connect(self.save_current_step_changes)
        self.edit_step_actuator_val.toggled.connect(self.save_current_step_changes)
        self.edit_step_wait_time_spin.valueChanged.connect(self.save_current_step_changes)
        self.edit_step_wait_until_op.currentTextChanged.connect(self.save_current_step_changes)
        self.btn_add_step_wait_until_cond.clicked.connect(self.add_step_condition)
        
        # Initialize visibility & status
        self.group_triggers.setVisible(False)
        self.btn_del_seq.setEnabled(False)
        self.step_edit_stack.setCurrentIndex(0)

    def refresh_logic_dropdowns(self):
        local_kit = self.edit_kit_name.text()
        sensors = [f"{local_kit} - {sw.edit_pin.text()}" for sw in self.sensor_widgets if sw.edit_pin.text()]
        actuators = [f"{local_kit} - {aw.edit_pin.text()}" for aw in self.actuator_widgets if aw.edit_pin.text()]
        
        # Update sequence triggers
        for i in range(self.triggers_layout.count()):
            w = self.triggers_layout.itemAt(i).widget()
            if isinstance(w, TriggerRow):
                curr_text = w.combo_sensor.currentText()
                w.combo_sensor.blockSignals(True)
                w.combo_sensor.clear()
                w.combo_sensor.addItems(sensors)
                w.combo_sensor.setCurrentText(curr_text)
                w.combo_sensor.blockSignals(False)
                
        # Update step actuator dropdown
        curr_act = self.edit_step_actuator_combo.currentText()
        self.edit_step_actuator_combo.blockSignals(True)
        self.edit_step_actuator_combo.clear()
        self.edit_step_actuator_combo.addItems(actuators)
        self.edit_step_actuator_combo.setCurrentText(curr_act)
        self.edit_step_actuator_combo.blockSignals(False)
        
        # Update step wait_until sensor dropdowns
        for i in range(self.step_wait_until_layout.count()):
            w = self.step_wait_until_layout.itemAt(i).widget()
            if isinstance(w, TriggerRow):
                curr_text = w.combo_sensor.currentText()
                w.combo_sensor.blockSignals(True)
                w.combo_sensor.clear()
                w.combo_sensor.addItems(sensors)
                w.combo_sensor.setCurrentText(curr_text)
                w.combo_sensor.blockSignals(False)

    def get_current_steps_list(self):
        row = self.list_sequences.currentRow()
        if row == 0:
            return self.logic_rules["start"]
        elif row > 0 and (row - 1) < len(self.logic_rules["sequences"]):
            return self.logic_rules["sequences"][row - 1]["steps"]
        return None

    def refresh_steps_list(self):
        self.list_steps.blockSignals(True)
        self.list_steps.clear()
        steps = self.get_current_steps_list()
        if steps is not None:
            for step in steps:
                text = self.get_step_display_text(step)
                self.list_steps.addItem(text)
        self.list_steps.blockSignals(False)
        self.update_step_edit_pane()

    def get_step_display_text(self, step):
        t = step.get("type")
        if t == "set_actuator":
            kit = step.get("kit", "")
            act = step.get("actuator", "None")
            val = step.get("value", False)
            if kit:
                return f"Set Actuator: [{kit}] - {act} -> {val}"
            return f"Set Actuator: {act} -> {val}"
        elif t == "wait_time":
            val = step.get("seconds", 0.0)
            return f"Wait Time: {val}s"
        elif t == "wait_until":
            op = step.get("operator", "AND")
            conds = step.get("conditions", [])
            cond_strs = []
            for c in conds:
                kit_str = f"[{c.get('kit')}] - " if c.get("kit") else ""
                cond_strs.append(f"{kit_str}{c.get('sensor')}=={c.get('value')}")
            cond_str = ", ".join(cond_strs)
            return f"Wait Until: {op} ({cond_str})"
        return "Unknown Step"

    def block_step_edit_signals(self, block):
        self.edit_step_actuator_combo.blockSignals(block)
        self.edit_step_actuator_val.blockSignals(block)
        self.edit_step_wait_time_spin.blockSignals(block)
        self.edit_step_wait_until_op.blockSignals(block)

    def update_step_edit_pane(self):
        step_row = self.list_steps.currentRow()
        steps = self.get_current_steps_list()
        
        if steps is None or step_row < 0 or step_row >= len(steps):
            self.step_edit_stack.setCurrentIndex(0)
            return
            
        step = steps[step_row]
        t = step.get("type")
        
        self.block_step_edit_signals(True)
        
        if t == "set_actuator":
            self.step_edit_stack.setCurrentIndex(1)
            self.edit_step_actuator_combo.clear()
            local_kit = self.edit_kit_name.text()
            actuators = [f"{local_kit} - {aw.edit_pin.text()}" for aw in self.actuator_widgets if aw.edit_pin.text()]
            self.edit_step_actuator_combo.addItems(actuators)
            
            target_kit = step.get("kit") or local_kit
            target_act = step.get("actuator", "")
            target_text = f"{target_kit} - {target_act}" if target_kit and target_act else target_act
            
            if target_text in actuators:
                self.edit_step_actuator_combo.setCurrentText(target_text)
            elif target_act in actuators:
                self.edit_step_actuator_combo.setCurrentText(target_act)
            else:
                matched = False
                for item in actuators:
                    if item.endswith(f" - {target_act}"):
                        self.edit_step_actuator_combo.setCurrentText(item)
                        matched = True
                        break
                if not matched and actuators:
                    self.edit_step_actuator_combo.setCurrentIndex(0)
            self.edit_step_actuator_val.setChecked(bool(step.get("value", False)))
            
        elif t == "wait_time":
            self.step_edit_stack.setCurrentIndex(2)
            self.edit_step_wait_time_spin.setValue(float(step.get("seconds", 0.0)))
            
        elif t == "wait_until":
            self.step_edit_stack.setCurrentIndex(3)
            self.edit_step_wait_until_op.setCurrentText(step.get("operator", "AND"))
            while self.step_wait_until_layout.count() > 0:
                child = self.step_wait_until_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
                    
            local_kit = self.edit_kit_name.text()
            sensors = [f"{local_kit} - {sw.edit_pin.text()}" for sw in self.sensor_widgets if sw.edit_pin.text()]
            for cond in step.get("conditions", []):
                row_widget = TriggerRow(
                    self,
                    sensors,
                    initial_sensor=cond.get("sensor", ""),
                    initial_value=cond.get("value", True),
                    on_delete=lambda w=None: self.delete_step_condition(row_widget),
                    on_changed=self.save_current_step_changes,
                    initial_kit=cond.get("kit")
                )
                self.step_wait_until_layout.addWidget(row_widget)
                
        else:
            self.step_edit_stack.setCurrentIndex(0)
            
        self.block_step_edit_signals(False)

    def delete_step_condition(self, row_widget):
        row_widget.deleteLater()
        QTimer.singleShot(50, self.save_current_step_changes)

    def save_current_step_changes(self):
        step_row = self.list_steps.currentRow()
        steps = self.get_current_steps_list()
        
        if steps is None or step_row < 0 or step_row >= len(steps):
            return
            
        step = steps[step_row]
        t = step.get("type")
        
        default_kit = ""
        if hasattr(self, "edit_kit_name"):
            default_kit = self.edit_kit_name.text()
        elif hasattr(self, "get_current_kit"):
            default_kit = self.get_current_kit()
            
        if t == "set_actuator":
            text = self.edit_step_actuator_combo.currentText()
            if " - " in text:
                parts = text.split(" - ", 1)
                step["kit"] = parts[0].strip()
                step["actuator"] = parts[1].strip()
            else:
                step["kit"] = default_kit
                step["actuator"] = text.strip()
            step["value"] = self.edit_step_actuator_val.isChecked()
        elif t == "wait_time":
            step["seconds"] = self.edit_step_wait_time_spin.value()
        elif t == "wait_until":
            step["operator"] = self.edit_step_wait_until_op.currentText()
            conds = []
            for i in range(self.step_wait_until_layout.count()):
                w = self.step_wait_until_layout.itemAt(i).widget()
                if isinstance(w, TriggerRow):
                    conds.append(w.get_data())
            step["conditions"] = conds
            
        self.list_steps.item(step_row).setText(self.get_step_display_text(step))
        self.update_json_preview()

    def save_trigger_changes(self):
        row = self.list_sequences.currentRow()
        if row <= 0:
            return
            
        seq_idx = row - 1
        if seq_idx < 0 or seq_idx >= len(self.logic_rules["sequences"]):
            return
            
        seq = self.logic_rules["sequences"][seq_idx]
        if "trigger" not in seq or not isinstance(seq["trigger"], dict):
            seq["trigger"] = {"operator": "AND", "conditions": []}
            
        seq["trigger"]["operator"] = self.combo_trigger_op.currentText()
        
        triggers = []
        for i in range(self.triggers_layout.count()):
            w = self.triggers_layout.itemAt(i).widget()
            if isinstance(w, TriggerRow):
                triggers.append(w.get_data())
        seq["trigger"]["conditions"] = triggers
        

    def add_trigger_condition(self):
        row = self.list_sequences.currentRow()
        if row <= 0:
            return
            
        local_kit = self.edit_kit_name.text()
        sensors = [f"{local_kit} - {sw.edit_pin.text()}" for sw in self.sensor_widgets if sw.edit_pin.text()]
        row_widget = TriggerRow(
            self,
            sensors,
            initial_sensor="",
            initial_value=True,
            on_delete=lambda w=None: self.delete_trigger_condition(row_widget),
            on_changed=self.save_trigger_changes
        )
        self.triggers_layout.addWidget(row_widget)
        self.save_trigger_changes()
        
    def delete_trigger_condition(self, row_widget):
        row_widget.deleteLater()
        QTimer.singleShot(50, self.save_trigger_changes)

    def on_sequence_selected(self, row):
        self.save_current_step_changes()
        
        if row == 0:
            self.group_triggers.setVisible(False)
            self.refresh_steps_list()
        elif row > 0 and (row - 1) < len(self.logic_rules["sequences"]):
            self.group_triggers.setVisible(True)
            seq = self.logic_rules["sequences"][row - 1]
            if "trigger" not in seq or not isinstance(seq["trigger"], dict):
                seq["trigger"] = {"operator": "AND", "conditions": []}
            
            self.combo_trigger_op.blockSignals(True)
            self.combo_trigger_op.setCurrentText(seq["trigger"].get("operator", "AND"))
            self.combo_trigger_op.blockSignals(False)
            
            while self.triggers_layout.count() > 0:
                child = self.triggers_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
                    
            local_kit = self.edit_kit_name.text()
            sensors = [f"{local_kit} - {sw.edit_pin.text()}" for sw in self.sensor_widgets if sw.edit_pin.text()]
            for cond in seq["trigger"].get("conditions", []):
                row_widget = TriggerRow(
                    self,
                    sensors,
                    initial_sensor=cond.get("sensor", ""),
                    initial_value=cond.get("value", True),
                    on_delete=lambda w=None: self.delete_trigger_condition(row_widget),
                    on_changed=self.save_trigger_changes,
                    initial_kit=cond.get("kit")
                )
                self.triggers_layout.addWidget(row_widget)
                
            self.refresh_steps_list()
        else:
            self.group_triggers.setVisible(False)
            self.list_steps.clear()
            self.step_edit_stack.setCurrentIndex(0)

    def add_sequence(self):
        name, ok = QInputDialog.getText(self, "Add Sequence", "Enter sequence name:")
        if ok and name.strip():
            name = name.strip()
            existing = ["Start Sequence"] + [s["name"] for s in self.logic_rules["sequences"]]
            if name in existing:
                QMessageBox.warning(self, "Warning", "A sequence with this name already exists.")
                return
            
            self.logic_rules["sequences"].append({
                "name": name,
                "trigger": {"operator": "AND", "conditions": []},
                "steps": []
            })
            self.list_sequences.addItem(name)
            self.list_sequences.setCurrentRow(self.list_sequences.count() - 1)
            self.update_json_preview()

    def delete_sequence(self):
        row = self.list_sequences.currentRow()
        if row <= 0:
            QMessageBox.warning(self, "Warning", "Cannot delete the permanent Start Sequence.")
            return
            
        seq_idx = row - 1
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to delete sequence '{self.logic_rules['sequences'][seq_idx]['name']}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            del self.logic_rules["sequences"][seq_idx]
            self.list_sequences.takeItem(row)
            self.list_sequences.setCurrentRow(row - 1)
            self.update_json_preview()

    def on_sequences_reordered(self, parent, start, end, destination, row):
        new_sequences = []
        for i in range(self.list_sequences.count()):
            item = self.list_sequences.item(i)
            name = item.text()
            if name == "Start Sequence":
                if i != 0:
                    self.list_sequences.blockSignals(True)
                    self.list_sequences.takeItem(i)
                    self.list_sequences.insertItem(0, item)
                    self.list_sequences.setCurrentRow(0)
                    self.list_sequences.blockSignals(False)
                continue
            for seq in self.logic_rules.get("sequences", []):
                if seq.get("name") == name:
                    new_sequences.append(seq)
                    break
        self.logic_rules["sequences"] = new_sequences
        self.update_json_preview()

    def add_set_actuator_step(self):
        steps = self.get_current_steps_list()
        if steps is not None:
            local_kit = self.edit_kit_name.text()
            actuators = [f"{local_kit} - {aw.edit_pin.text()}" for aw in self.actuator_widgets if aw.edit_pin.text()]
            act_text = actuators[0] if actuators else ""
            if " - " in act_text:
                parts = act_text.split(" - ", 1)
                kit = parts[0].strip()
                act = parts[1].strip()
            else:
                kit = local_kit
                act = act_text
            steps.append({
                "type": "set_actuator",
                "kit": kit,
                "actuator": act,
                "value": False
            })
            self.refresh_steps_list()
            self.list_steps.setCurrentRow(len(steps) - 1)
            self.update_json_preview()

    def add_wait_time_step(self):
        steps = self.get_current_steps_list()
        if steps is not None:
            steps.append({
                "type": "wait_time",
                "seconds": 1.0
            })
            self.refresh_steps_list()
            self.list_steps.setCurrentRow(len(steps) - 1)
            self.update_json_preview()

    def add_wait_until_step(self):
        steps = self.get_current_steps_list()
        if steps is not None:
            steps.append({
                "type": "wait_until",
                "operator": "AND",
                "conditions": []
            })
            self.refresh_steps_list()
            self.list_steps.setCurrentRow(len(steps) - 1)
            self.update_json_preview()

    def add_step_condition(self):
        step_row = self.list_steps.currentRow()
        steps = self.get_current_steps_list()
        if steps is None or step_row < 0 or step_row >= len(steps):
            return
        step = steps[step_row]
        if step.get("type") != "wait_until":
            return
            
        local_kit = self.edit_kit_name.text()
        sensors = [f"{local_kit} - {sw.edit_pin.text()}" for sw in self.sensor_widgets if sw.edit_pin.text()]
        row_widget = TriggerRow(
            self,
            sensors,
            initial_sensor="",
            initial_value=True,
            on_delete=lambda w=None: self.delete_step_condition(row_widget),
            on_changed=self.save_current_step_changes
        )
        self.step_wait_until_layout.addWidget(row_widget)
        self.save_current_step_changes()

    def move_step_up(self):
        step_row = self.list_steps.currentRow()
        steps = self.get_current_steps_list()
        if steps is not None and step_row > 0:
            steps[step_row], steps[step_row - 1] = steps[step_row - 1], steps[step_row]
            self.refresh_steps_list()
            self.list_steps.setCurrentRow(step_row - 1)
            self.update_json_preview()

    def move_step_down(self):
        step_row = self.list_steps.currentRow()
        steps = self.get_current_steps_list()
        if steps is not None and 0 <= step_row < len(steps) - 1:
            steps[step_row], steps[step_row + 1] = steps[step_row + 1], steps[step_row]
            self.refresh_steps_list()
            self.list_steps.setCurrentRow(step_row + 1)
            self.update_json_preview()

    def delete_step(self):
        step_row = self.list_steps.currentRow()
        steps = self.get_current_steps_list()
        if steps is not None and 0 <= step_row < len(steps):
            reply = QMessageBox.question(
                self, "Confirm Delete",
                "Are you sure you want to delete this step?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                del steps[step_row]
                self.refresh_steps_list()
                if step_row < len(steps):
                    self.list_steps.setCurrentRow(step_row)
                elif len(steps) > 0:
                    self.list_steps.setCurrentRow(len(steps) - 1)
                self.update_json_preview()

    def manage_deployed_aas(self):
        try:
            res = requests.get("http://localhost:8082/shell-descriptors", timeout=5)
            shells = res.json().get("result", []) if res.status_code == 200 else []
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not connect to registry: {e}")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Manage Deployed AAS")
        dlg.resize(600, 400)
        layout = QVBoxLayout(dlg)

        layout.addWidget(QLabel("Select a Deployed AAS to Clone/Backup/Delete, or click 'Restore (Upload)':"))
        
        list_widget = QListWidget()
        layout.addWidget(list_widget)

        def refresh_list(initial_shells=None):
            list_widget.clear()
            try:
                if initial_shells is not None:
                    curr_shells = initial_shells
                else:
                    res_refresh = requests.get("http://localhost:8082/shell-descriptors", timeout=5)
                    curr_shells = res_refresh.json().get("result", [])
                for s in curr_shells:
                    item = QListWidgetItem(f"{s.get('idShort')} ({s.get('id')})")
                    item.setData(Qt.ItemDataRole.UserRole, s)
                    list_widget.addItem(item)
            except Exception as e:
                QMessageBox.warning(dlg, "Error", f"Failed to refresh list: {e}")

        refresh_list(initial_shells=shells)

        btn_layout = QHBoxLayout()
        btn_clone = QPushButton("Clone & Edit")
        btn_clone.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; min-height: 35px;")
        
        btn_backup = QPushButton("Backup Selected")
        btn_backup.setStyleSheet("background-color: #2980b9; color: white; font-weight: bold; min-height: 35px;")
        
        btn_backup_all = QPushButton("Backup All")
        btn_backup_all.setStyleSheet("background-color: #34495e; color: white; font-weight: bold; min-height: 35px;")
        
        btn_restore = QPushButton("Restore (Upload)")
        btn_restore.setStyleSheet("background-color: #16a085; color: white; font-weight: bold; min-height: 35px;")
        
        btn_delete = QPushButton("Delete AAS")
        btn_delete.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold; min-height: 35px;")
        
        btn_close = QPushButton("Close")
        btn_close.setStyleSheet("min-height: 35px;")

        btn_layout.addWidget(btn_clone)
        btn_layout.addWidget(btn_backup)
        btn_layout.addWidget(btn_backup_all)
        btn_layout.addWidget(btn_restore)
        btn_layout.addWidget(btn_delete)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

        def on_clone_clicked():
            item = list_widget.currentItem()
            if not item:
                QMessageBox.warning(dlg, "Warning", "Please select an AAS to clone.")
                return
            shell_desc = item.data(Qt.ItemDataRole.UserRole)
            dlg.accept()
            self.load_cloned_aas(shell_desc)

        def on_delete_clicked():
            item = list_widget.currentItem()
            if not item:
                QMessageBox.warning(dlg, "Warning", "Please select an AAS to delete.")
                return
            shell_desc = item.data(Qt.ItemDataRole.UserRole)
            id_short = shell_desc.get("idShort", "Unknown")
            shell_id = shell_desc.get("id")

            reply = QMessageBox.question(
                dlg, "Confirm Delete",
                f"Are you sure you want to delete AAS '{id_short}' and all its associated submodels?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.delete_aas_by_id(shell_id)
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                QMessageBox.information(dlg, "Success", f"AAS '{id_short}' and its associated submodels deleted successfully.")
                refresh_list()
            except Exception as e:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                traceback.print_exc()
                QMessageBox.critical(dlg, "Error", f"Failed to delete AAS: {e}")
            finally:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()

        def get_backup_data(shell_desc):
            shell_id = shell_desc.get("id")
            encoded_shell_id = base64.urlsafe_b64encode(shell_id.encode()).decode().rstrip("=")
            
            # Fetch shell to get submodels
            shell_res = requests.get(f"http://localhost:8081/shells/{encoded_shell_id}", timeout=5)
            if shell_res.status_code != 200:
                raise Exception(f"Failed to fetch Shell (Code {shell_res.status_code})")
            shell_data = shell_res.json()
            
            submodel_refs = shell_data.get("submodels", [])
            submodels_data = {}
            for ref in submodel_refs:
                keys = ref.get("keys", [])
                if keys:
                    sm_id = keys[0].get("value")
                    encoded_sm_id = base64.urlsafe_b64encode(sm_id.encode()).decode().rstrip("=")
                    sm_res = requests.get(f"http://localhost:8081/submodels/{encoded_sm_id}", timeout=5)
                    if sm_res.status_code == 200:
                        sm_data = sm_res.json()
                        submodels_data[sm_data.get("idShort", "")] = sm_data

            vis_data = next((v for k,v in submodels_data.items() if "VisualStructure" in k or "Visual" in k), None)
            beh_data = next((v for k,v in submodels_data.items() if "BehaviorMapping" in k or "Behavior" in k), None)
            aid_data = next((v for k,v in submodels_data.items() if "AssetInterfacesDescription" in k), None)
            logic_data = next((v for k,v in submodels_data.items() if "ControlLogic" in k or "Logic" in k), None)
            control_logic = {"start": [], "sequences": []}
            if logic_data:
                for el in logic_data.get("submodelElements", []):
                    if el.get("idShort") == "Rules":
                        r_str = el.get("value", "")
                        if r_str:
                            try:
                                loaded_l = json.loads(r_str)
                                if isinstance(loaded_l, dict):
                                    control_logic = loaded_l
                            except Exception:
                                pass
            
            # Extract communication parameters if AssetInterfacesDescription exists
            comm_protocol = "Sockets"
            comm_ip = "127.0.0.1"
            comm_port = 5000
            if aid_data:
                aid_props = {el["idShort"]: el.get("value") for el in aid_data.get("submodelElements", []) if el.get("modelType") == "Property"}
                comm_protocol = str(aid_props.get("Protocol", "Sockets"))
                comm_ip = str(aid_props.get("EndpointIP", "127.0.0.1"))
                try:
                    comm_port = int(aid_props.get("EndpointPort", 5000))
                except (ValueError, TypeError):
                    comm_port = 5000
                if hasattr(self, "combo_comm_protocol"):
                    self.combo_comm_protocol.setCurrentText(comm_protocol)
                if hasattr(self, "edit_comm_ip"):
                    self.edit_comm_ip.setText(comm_ip)
                if hasattr(self, "spin_comm_port"):
                    self.spin_comm_port.setValue(comm_port)

            # Gather config parameters (equivalent to clone parsing)
            kit_name = shell_desc.get("idShort", "ClonedKit")
            description = ""
            if "KitConfiguration" in submodels_data:
                cfg = submodels_data["KitConfiguration"]
                for el in cfg.get("submodelElements", []):
                    if el["idShort"] == "Description":
                        description = el.get("value", "")

            pos_val = [0.0]*3; rot_val = [0.0]*3; scale_val = [1.0]*3
            mesh_href = None
            is_dynamic = False
            if vis_data:
                for el in vis_data.get("submodelElements", []):
                    if el["modelType"] == "SubmodelElementCollection": 
                        for sel in el.get("value", []):
                            if sel["idShort"] == "Transform":
                                for t in sel.get("value", []):
                                    v = float(t.get("value", 0))
                                    if t["idShort"] == "PosX": pos_val[0] = v
                                    elif t["idShort"] == "PosY": pos_val[1] = v
                                    elif t["idShort"] == "PosZ": pos_val[2] = v
                                    elif t["idShort"] == "RotX": rot_val[0] = v
                                    elif t["idShort"] == "RotY": rot_val[1] = v
                                    elif t["idShort"] == "RotZ": rot_val[2] = v
                                    elif t["idShort"] == "ScaleX": scale_val[0] = v
                                    elif t["idShort"] == "ScaleY": scale_val[1] = v
                                    elif t["idShort"] == "ScaleZ": scale_val[2] = v
                            elif sel["idShort"] == "Mesh" and sel["modelType"] == "File":
                                mesh_href = sel.get("value")
                            elif sel["idShort"] == "DynamicObject" and sel["modelType"] == "Property":
                                is_dynamic = str(sel.get("value", "False")).lower() == "true"

            actuators = []
            sensors = []
            act_types = ["Conveyor", "RotateContinuous", "TranslateContinuous", "Piston", "Linear"]
            sens_types = ["TriggerZone", "Presence", "Proximity"]
            
            if beh_data:
                for el in beh_data.get("submodelElements", []):
                    if el["modelType"] == "SubmodelElementCollection":
                        props = {p["idShort"]: p.get("value") for p in el.get("value", []) if p["modelType"] == "Property"}
                        params = {}
                        for p in el.get("value", []):
                            if p["idShort"] == "Parameters":
                                params = {pp["idShort"]: pp.get("value") for pp in p.get("value", []) if pp["modelType"] == "Property"}
                                
                        b_type = props.get("Type", ""); pin_id = props.get("PinID", ""); node = props.get("Component", "")
                        
                        if b_type in act_types:
                            act_data = {
                                "pin_id": pin_id, "node": node, "behavior": b_type,
                                "axis": [float(params.get("AxisX", 0)), float(params.get("AxisY", 0)), float(params.get("AxisZ", 0))],
                                "speed": float(params.get("Speed", 1.0)),
                                "visible": str(params.get("Visible", "True")).lower() == "true"
                            }
                            if "StopSensorComponent" in params:
                                act_data["stop_sensor"] = params["StopSensorComponent"]
                              
                            actuators.append(act_data)
                        elif b_type in sens_types:
                            r_enabled = str(params.get("RealSensorEnabled", "False")).lower() == "true" if isinstance(params.get("RealSensorEnabled"), str) else bool(params.get("RealSensorEnabled", False))
                            sens_data = {
                                "pin_id": pin_id, "node": node, "behavior": b_type,
                                "target_filter": params.get("TargetFilter", "Metal"),
                                "visible": str(params.get("Visible", "False")).lower() == "true",
                                "active_low": str(params.get("ActiveLow", "False")).lower() == "true",
                                "snap_product": "SnapTarget" in params,
                                "real_sensor_enabled": r_enabled,
                                "real_port_id": str(params.get("RealPortID", ""))
                            }
                            if "MoveTarget" in params:
                                sens_data["move_target"] = params["MoveTarget"]
                            if "MoveOnState" in params:
                                sens_data["move_on_state"] = str(params["MoveOnState"]).lower() == "true"
                            if "MoveMode" in params:
                                sens_data["move_mode"] = str(params.get("MoveMode", "Both"))
                            if "MovePosX" in params:
                                sens_data["move_position"] = [float(params.get("MovePosX", 0)), float(params.get("MovePosY", 0)), float(params.get("MovePosZ", 0))]
                            if "MoveRotX" in params:
                                sens_data["move_rotation"] = [float(params.get("MoveRotX", 0)), float(params.get("MoveRotY", 0)), float(params.get("MoveRotZ", 0))]
                            if "MoveDuration" in params:
                                sens_data["move_duration"] = float(params.get("MoveDuration", 0.5))
                            sensors.append(sens_data)

            # Get GLB attachment
            glb_bytes = b""
            if mesh_href and vis_data:
                vis_id = vis_data.get("id")
                encoded_vis_id = base64.urlsafe_b64encode(vis_id.encode()).decode().rstrip("=")
                coll_id_short = None
                for el in vis_data.get("submodelElements", []):
                    if el["modelType"] == "SubmodelElementCollection": 
                        for sel in el.get("value", []):
                            if sel["idShort"] == "Mesh":
                                coll_id_short = el["idShort"]
                                break
                if coll_id_short:
                    dl_url = f"http://localhost:8081/submodels/{encoded_vis_id}/submodel-elements/{coll_id_short}.Mesh/attachment"
                    m_res = requests.get(dl_url, timeout=5)
                    if m_res.status_code == 200:
                        glb_bytes = m_res.content

            # Assemble backup structure
            return {
                "version": "1.0",
                "kit_name": kit_name,
                "aas_id": shell_desc.get("id", f"https://acplt.org/AAS_{kit_name}"),
                "description": description,
                "comm_protocol": comm_protocol,
                "comm_ip": comm_ip,
                "comm_port": comm_port,
                "control_logic": control_logic,
                "config_data": {
                    "kit_name": kit_name,
                    "aas_id": kit_name,
                    "description": description,
                    "dynamic": is_dynamic,
                    "comm_protocol": comm_protocol,
                    "comm_ip": comm_ip,
                    "comm_port": comm_port,
                    "pos": pos_val,
                    "rot": rot_val,
                    "scale": scale_val,
                    "actuators": actuators,
                    "sensors": sensors,
                    "control_logic": control_logic
                },
                "glb_filename": f"{kit_name}.glb",
                "glb_base64": base64.b64encode(glb_bytes).decode('utf-8')
            }

        def on_backup_clicked():
            item = list_widget.currentItem()
            if not item:
                QMessageBox.warning(dlg, "Warning", "Please select an AAS to backup.")
                return
            shell_desc = item.data(Qt.ItemDataRole.UserRole)
            id_short = shell_desc.get("idShort", "Unknown")

            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                backup_data = get_backup_data(shell_desc)
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()

                save_path, _ = QFileDialog.getSaveFileName(dlg, "Save AAS Backup", f"{id_short}_backup.json", "JSON Files (*.json)")
                if save_path:
                    with open(save_path, "w", encoding="utf-8") as f:
                        json.dump(backup_data, f, indent=2)
                    QMessageBox.information(dlg, "Success", "Backup saved successfully!")
            except Exception as e:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                traceback.print_exc()
                QMessageBox.critical(dlg, "Error", f"Failed to backup AAS: {e}")
            finally:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()

        def on_backup_all_clicked():
            if list_widget.count() == 0:
                QMessageBox.warning(dlg, "Warning", "No active AAS kits to backup.")
                return

            dest_dir = QFileDialog.getExistingDirectory(dlg, "Select Directory to Save Backups")
            if not dest_dir:
                return

            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            success_count = 0
            fail_messages = []
            
            try:
                for i in range(list_widget.count()):
                    item = list_widget.item(i)
                    shell_desc = item.data(Qt.ItemDataRole.UserRole)
                    id_short = shell_desc.get("idShort", f"Kit_{i}")
                    
                    try:
                        backup_data = get_backup_data(shell_desc)
                        save_path = os.path.join(dest_dir, f"{id_short}_backup.json")
                        with open(save_path, "w", encoding="utf-8") as f:
                            json.dump(backup_data, f, indent=2)
                        success_count += 1
                    except Exception as e:
                        fail_messages.append(f"{id_short}: {e}")
                        
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                
                if success_count > 0:
                    msg = f"Successfully backed up {success_count} kits to:\n{dest_dir}"
                    if fail_messages:
                        msg += "\n\nFailures:\n" + "\n".join(fail_messages)
                    QMessageBox.information(dlg, "Backup All Success", msg)
                else:
                    QMessageBox.warning(dlg, "Backup All Failed", "Failed to backup any kits.\n\nErrors:\n" + "\n".join(fail_messages))
            except Exception as e:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                QMessageBox.critical(dlg, "Error", f"An unexpected error occurred during Backup All: {e}")
            finally:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()

        def on_restore_clicked():
            open_paths, _ = QFileDialog.getOpenFileNames(
                dlg, "Open AAS Backup / Package", "",
                "AAS Files (*.json *.aasx);;JSON Files (*.json);;AASX Packages (*.aasx);;All Files (*.*)"
            )
            if not open_paths:
                return

            def deploy_raw_aasx(path):
                with open(path, 'rb') as f:
                    fn = os.path.basename(path)
                    res = requests.post("http://localhost:8081/upload", files={'file': (fn, f, 'application/octet-stream')}, timeout=15)
                if res.status_code not in [200, 201, 204, 409]:
                    raise Exception(f"Failed to upload AASX (HTTP {res.status_code}): {res.text}")
                try:
                    s_res = requests.get("http://localhost:8081/shells", timeout=5)
                    if s_res.status_code == 200:
                        shells = s_res.json().get("result", [])
                        for s in shells:
                            sm_descs = []
                            for ref in s.get("submodels", []):
                                keys = ref.get("keys", [])
                                if keys:
                                    sm_id = keys[0].get("value")
                                    sm_descs.append({
                                        "idShort": sm_id.split('/')[-1].split('_')[-1],
                                        "id": sm_id,
                                        "endpoints": [{"interface": "SUBMODEL-3.0", "protocolInformation": {"href": f"http://localhost:8081/submodels/{sm_id}", "endpointProtocol": "HTTP"}}]
                                    })
                            reg_payload = {
                                "id": s.get("id"),
                                "idShort": s.get("idShort", "AAS"),
                                "assetKind": s.get("assetInformation", {}).get("assetKind", "Instance"),
                                "globalAssetId": s.get("assetInformation", {}).get("globalAssetId", f"http://acplt.org/Assets/{s.get('idShort', 'AAS')}"),
                                "submodelDescriptors": sm_descs,
                                "endpoints": [{"interface": "AAS-3.0", "protocolInformation": {"href": f"http://localhost:8081/shells/{s.get('id')}", "endpointProtocol": "HTTP"}}]
                            }
                            requests.post("http://localhost:8082/shell-descriptors", json=reg_payload, timeout=5)
                except Exception as ex:
                    print(f"[REGISTRY SYNC WARNING] {ex}")

            def process_backup_file(path):
                if path.lower().endswith(".aasx"):
                    deploy_raw_aasx(path)
                    return {"type": "aasx"}

                with open(path, "r", encoding="utf-8") as f:
                    b_data = json.load(f)

                is_env = isinstance(b_data, dict) and ("assetAdministrationShells" in b_data or ("submodels" in b_data and "config_data" not in b_data))
                is_single_shell = isinstance(b_data, dict) and (b_data.get("modelType") == "AssetAdministrationShell" or ("assetInformation" in b_data and "id" in b_data and "config_data" not in b_data))
                is_single_sm = isinstance(b_data, dict) and (b_data.get("modelType") == "Submodel" or ("submodelElements" in b_data and "config_data" not in b_data))

                if is_env or is_single_shell or is_single_sm:
                    if is_single_sm:
                        sms = [b_data]
                        shells = []
                    elif is_single_shell:
                        sms = []
                        shells = [b_data]
                    else:
                        sms = b_data.get("submodels", [])
                        shells = b_data.get("assetAdministrationShells", [])

                    for sm in sms:
                        sm_id = sm.get("id")
                        r = requests.post("http://localhost:8081/submodels", json=sm, headers={"Content-Type": "application/json"}, timeout=5)
                        if r.status_code == 409 and sm_id:
                            enc = base64.urlsafe_b64encode(sm_id.encode()).decode().rstrip("=")
                            requests.put(f"http://localhost:8081/submodels/{enc}", json=sm, headers={"Content-Type": "application/json"}, timeout=5)

                    for sh in shells:
                        sh_id = sh.get("id")
                        r = requests.post("http://localhost:8081/shells", json=sh, headers={"Content-Type": "application/json"}, timeout=5)
                        if r.status_code == 409 and sh_id:
                            enc = base64.urlsafe_b64encode(sh_id.encode()).decode().rstrip("=")
                            requests.put(f"http://localhost:8081/shells/{enc}", json=sh, headers={"Content-Type": "application/json"}, timeout=5)

                        sm_descs = []
                        for ref in sh.get("submodels", []):
                            keys = ref.get("keys", [])
                            if keys:
                                sm_val = keys[0].get("value")
                                sm_name = sm_val.split('/')[-1].split('_')[-1]
                                sm_descs.append({
                                    "idShort": sm_name,
                                    "id": sm_val,
                                    "endpoints": [{"interface": "SUBMODEL-3.0", "protocolInformation": {"href": f"http://localhost:8081/submodels/{sm_val}", "endpointProtocol": "HTTP"}}]
                                })
                        reg_payload = {
                            "id": sh.get("id"),
                            "idShort": sh.get("idShort", "ImportedAAS"),
                            "assetKind": sh.get("assetInformation", {}).get("assetKind", "Instance"),
                            "globalAssetId": sh.get("assetInformation", {}).get("globalAssetId", f"http://acplt.org/Assets/{sh.get('idShort', 'AAS')}"),
                            "submodelDescriptors": sm_descs,
                            "endpoints": [{"interface": "AAS-3.0", "protocolInformation": {"href": f"http://localhost:8081/shells/{sh.get('id')}", "endpointProtocol": "HTTP"}}]
                        }
                        requests.post("http://localhost:8082/shell-descriptors", json=reg_payload, timeout=5)
                    return {"type": "direct_deployed"}

                cfg_data = b_data.get("config_data")
                if not cfg_data:
                    fn_base = os.path.splitext(os.path.basename(path))[0]
                    k_name = b_data.get("kit_name") or b_data.get("idShort") or fn_base
                    cfg_data = {
                        "kit_name": k_name,
                        "aas_id": b_data.get("aas_id") or b_data.get("id") or f"https://acplt.org/AAS_{sanitize_aas_id(k_name)}",
                        "description": b_data.get("description", ""),
                        "dynamic": b_data.get("dynamic", False),
                        "comm_protocol": b_data.get("comm_protocol", "Sockets"),
                        "comm_ip": b_data.get("comm_ip", "127.0.0.1"),
                        "comm_port": b_data.get("comm_port", 5000),
                        "pos": b_data.get("pos", [0.0, 0.0, 0.0]),
                        "rot": b_data.get("rot", [0.0, 0.0, 0.0]),
                        "scale": b_data.get("scale", [1.0, 1.0, 1.0]),
                        "actuators": b_data.get("actuators", []),
                        "sensors": b_data.get("sensors", [])
                    }

                logic = b_data.get("control_logic") or b_data.get("rules") or cfg_data.get("control_logic")
                if logic:
                    cfg_data["control_logic"] = logic
                elif "start" in b_data or "sequences" in b_data:
                    cfg_data["control_logic"] = {"start": b_data.get("start", []), "sequences": b_data.get("sequences", [])}
                elif "start" in cfg_data or "sequences" in cfg_data:
                    cfg_data["control_logic"] = {"start": cfg_data.get("start", []), "sequences": cfg_data.get("sequences", [])}

                glb_b64 = b_data.get("glb_base64", "")
                if glb_b64:
                    import tempfile
                    tp = os.path.join(tempfile.gettempdir(), b_data.get("glb_filename", "restored.glb"))
                    glb_bytes = base64.b64decode(glb_b64.encode('utf-8'))
                    with open(tp, "wb") as f:
                        f.write(glb_bytes)
                    cfg_data["glb_path"] = tp
                else:
                    cfg_data["glb_path"] = None

                return {"type": "config_data", "data": cfg_data}

            if len(open_paths) == 1:
                path = open_paths[0]
                try:
                    res = process_backup_file(path)
                except Exception as e:
                    QMessageBox.critical(dlg, "Error", f"Failed to process file: {e}")
                    return

                if res.get("type") in ["aasx", "direct_deployed"]:
                    refresh_list()
                    QMessageBox.information(dlg, "Success", f"AAS file '{os.path.basename(path)}' deployed directly to BaSyx successfully.")
                    return

                config_data = res["data"]
                reply = QMessageBox.question(
                    dlg, "Restore Options",
                    "Would you like to load this backup into the Wizard for editing (Yes) or deploy it directly to the server (No)?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    return

                if reply == QMessageBox.StandardButton.Yes:
                    self.restore_wizard_state(config_data)
                    dlg.accept()
                    QMessageBox.information(self, "Success", "Backup loaded into Wizard successfully.")
                else:
                    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                    try:
                        curr_state = self.save_wizard_state()
                        self.restore_wizard_state(config_data)
                        self.deploy_to_aas(silent=True)
                        self.restore_wizard_state(curr_state)
                        while QApplication.overrideCursor() is not None:
                            QApplication.restoreOverrideCursor()
                        QMessageBox.information(dlg, "Success", "AAS deployed directly to BaSyx successfully.")
                        refresh_list()
                    except Exception as ex:
                        while QApplication.overrideCursor() is not None:
                            QApplication.restoreOverrideCursor()
                        QMessageBox.critical(dlg, "Error", f"Direct deployment failed: {ex}")
                    finally:
                        while QApplication.overrideCursor() is not None:
                            QApplication.restoreOverrideCursor()
            else:
                reply = QMessageBox.question(
                    dlg, "Restore Multiple Files",
                    f"You have selected {len(open_paths)} files. These will be deployed directly to the BaSyx server. Do you want to proceed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                success_count = 0
                fail_messages = []
                curr_state = self.save_wizard_state()
                
                try:
                    for path in open_paths:
                        fn = os.path.basename(path)
                        try:
                            res = process_backup_file(path)
                            if res.get("type") in ["aasx", "direct_deployed"]:
                                success_count += 1
                            else:
                                self.restore_wizard_state(res["data"])
                                self.deploy_to_aas(silent=True)
                                success_count += 1
                        except Exception as e:
                            fail_messages.append(f"{fn}: {e}")
                    
                    self.restore_wizard_state(curr_state)
                    while QApplication.overrideCursor() is not None:
                        QApplication.restoreOverrideCursor()

                    if success_count > 0:
                        msg = f"Successfully deployed {success_count} AAS file(s) directly to BaSyx!"
                        if fail_messages:
                            msg += "\n\nFailures:\n" + "\n".join(fail_messages)
                        QMessageBox.information(dlg, "Restore Multiple Success", msg)
                        refresh_list()
                    else:
                        QMessageBox.warning(dlg, "Restore Multiple Failed", "Failed to deploy any files.\n\nErrors:\n" + "\n".join(fail_messages))
                except Exception as e:
                    self.restore_wizard_state(curr_state)
                    while QApplication.overrideCursor() is not None:
                        QApplication.restoreOverrideCursor()
                    QMessageBox.critical(dlg, "Error", f"An unexpected error occurred during restore: {e}")
                finally:
                    while QApplication.overrideCursor() is not None:
                        QApplication.restoreOverrideCursor()

        btn_clone.clicked.connect(on_clone_clicked)
        btn_backup.clicked.connect(on_backup_clicked)
        btn_backup_all.clicked.connect(on_backup_all_clicked)
        btn_restore.clicked.connect(on_restore_clicked)
        btn_delete.clicked.connect(on_delete_clicked)
        btn_close.clicked.connect(dlg.reject)

        dlg.exec()

    def preview_deployed_layout(self):
        try:
            res = requests.get("http://localhost:8082/shell-descriptors", timeout=5)
            shells = res.json().get("result", [])
            if not shells:
                QMessageBox.information(self, "Info", "No AAS found on server.")
                return
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not connect to registry: {e}")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.plotter.clear()
        self.plotter.enable_eye_dome_lighting()
        self.plotter.enable_shadows()
        self.kill_overlays()
        self.meshes = {}
        self.actors = {}
        self.node_tree.clear()
        self.glb_path = None
        self.lbl_glb_path.setText("No file selected (Layout Preview Mode)")

        import trimesh.transformations as tf
        import numpy as np
        loaded_count = 0

        try:
            for s in shells:
                shell_id = s.get("id")
                id_short = s.get("idShort", "AAS")
                encoded_shell_id = base64.urlsafe_b64encode(shell_id.encode()).decode().rstrip("=")

                # Fetch shell details
                shell_res = requests.get(f"http://localhost:8081/shells/{encoded_shell_id}", timeout=5)
                if shell_res.status_code != 200: continue
                shell_data = shell_res.json()

                # Fetch submodel refs
                submodel_refs = shell_data.get("submodels", [])
                submodels_data = {}
                for ref in submodel_refs:
                    keys = ref.get("keys", [])
                    if keys:
                        sm_id = keys[0].get("value")
                        encoded_sm_id = base64.urlsafe_b64encode(sm_id.encode()).decode().rstrip("=")
                        sm_res = requests.get(f"http://localhost:8081/submodels/{encoded_sm_id}", timeout=5)
                        if sm_res.status_code == 200:
                            sm_data = sm_res.json()
                            submodels_data[sm_data.get("idShort", "")] = sm_data

                vis_data = next((v for k,v in submodels_data.items() if "VisualStructure" in k or "Visual" in k), None)
                
                # Check if it is a robot (URDF) without visual structure
                if "PositionData" in submodels_data and not vis_data:
                    pos_data = submodels_data["PositionData"]
                    pos_val = [0.0]*3; rot_val = [0.0]*3; scale_val = [1.0]*3
                    for el in pos_data.get("submodelElements", []):
                        if el["modelType"] == "Property":
                            try:
                                v = float(el.get("value", 0))
                                id_s = el["idShort"]
                                if id_s == "Pos_X": pos_val[0] = v
                                elif id_s == "Pos_Y": pos_val[1] = v
                                elif id_s == "Pos_Z": pos_val[2] = v
                                elif id_s == "Rot_X": rot_val[0] = v
                                elif id_s == "Rot_Y": rot_val[1] = v
                                elif id_s == "Rot_Z": rot_val[2] = v
                                elif id_s == "Scale_X": scale_val[0] = v
                                elif id_s == "Scale_Y": scale_val[1] = v
                                elif id_s == "Scale_Z": scale_val[2] = v
                            except:
                                pass
                    if "GeometryData" in submodels_data:
                        geo_data = submodels_data["GeometryData"]
                        geo_id = geo_data.get("id")
                        encoded_geo_id = base64.urlsafe_b64encode(geo_id.encode()).decode().rstrip("=")
                        
                        import tempfile
                        temp_robot_dir = tempfile.mkdtemp()
                        urdf_rel_path = None
                        urdf_file_element = None
                        for el in geo_data.get("submodelElements", []):
                            if el["modelType"] == "File":
                                val = el.get("value", "")
                                if val.lower().endswith(".urdf") or el.get("idShort", "").lower().endswith("_urdf") or el.get("idShort", "").lower().endswith("_urdf_dot_urdf"):
                                    urdf_file_element = el
                                    urdf_rel_path = get_file_rel_path(el)
                                        
                        if urdf_file_element and urdf_rel_path:
                            for el in geo_data.get("submodelElements", []):
                                if el["modelType"] == "File":
                                    rel_path = get_file_rel_path(el)
                                    id_short_f = el.get("idShort")
                                    dl_url = f"http://localhost:8081/submodels/{encoded_geo_id}/submodel-elements/{id_short_f}/attachment"
                                    m_res = requests.get(dl_url, timeout=5)
                                    if m_res.status_code == 200:
                                        dest_path = os.path.join(temp_robot_dir, rel_path.replace("/", os.sep))
                                        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                        with open(dest_path, "wb") as f: f.write(m_res.content)
                                    else:
                                        print(f"[DOWNLOAD ERROR] Failed to download {id_short_f} (HTTP Code: {m_res.status_code})")
                                        raise Exception(f"Failed to download robot mesh/file '{id_short_f}' (HTTP Code {m_res.status_code})")
                                        
                            target_urdf = os.path.join(temp_robot_dir, urdf_rel_path.replace("/", os.sep))
                            try:
                                robot_obj = URDF.load(target_urdf)
                                fk = robot_obj.visual_geometry_fk()
                                
                                geom_to_visual = {}
                                for l in robot_obj.links:
                                    for v in l.visuals:
                                        if v.geometry:
                                            geom_to_visual[v.geometry] = v
                                            
                                # Convert coordinates to match Unity left-handed coordinate system (reflected Z-flip)
                                pos_pv = [-pos_val[1], pos_val[2], -pos_val[0]]
                                rz, rx, ry = rot_val[0], rot_val[1], 90.0 - rot_val[2]
                                scale_pv = [scale_val[1], scale_val[2], scale_val[0]]
                                
                                T_l = tf.translation_matrix(pos_pv)
                                R_l = tf.euler_matrix(np.radians(rz), np.radians(rx), np.radians(ry), 'szxy')
                                S_l = np.diag([scale_pv[0], scale_pv[1], scale_pv[2], 1.0])
                                R_z_to_y = tf.euler_matrix(np.radians(-90), 0, 0, 'sxyz')
                                M_layout = tf.concatenate_matrices(T_l, R_l, S_l) @ R_z_to_y
                                
                                for geom, geom_tf in fk.items():
                                    # Get meshes from urdfpy geometry
                                    meshes = getattr(geom, "meshes", None)
                                    if meshes is None or len(meshes) == 0:
                                        # Try fallback for Mesh type
                                        if type(geom).__name__ == "Mesh" and getattr(geom, "filename", None):
                                            try:
                                                loaded = trimesh.load(geom.filename)
                                                if isinstance(loaded, trimesh.Scene):
                                                     m = loaded.dump(concatenate=True)
                                                else:
                                                     m = loaded
                                                if hasattr(geom, 'scale') and geom.scale is not None:
                                                    m.apply_scale(geom.scale)
                                                meshes = [m]
                                            except Exception as e_fallback:
                                                print(f"Fallback mesh load failed: {e_fallback}")
                                                
                                    if meshes is None or len(meshes) == 0:
                                        continue
                                        
                                    # Apply transform & render
                                    scale_matrix = np.eye(4)
                                    if hasattr(geom, 'scale') and geom.scale is not None:
                                        s = geom.scale
                                        scale_matrix = np.diag([s[0], s[1], s[2], 1.0])
                                    
                                    total_transform = M_layout @ geom_tf @ scale_matrix
                                    
                                    for m in meshes:
                                        if m is None: continue
                                        m_copy = m.copy()
                                        m_copy.apply_transform(total_transform)
                                        pm = pv.wrap(m_copy)
                                        actor_name = f"layout_{id_short}_{getattr(geom, 'name', 'link')}_{id(m)}"
                                        
                                        # Get color matching Unity/URDF
                                        visual_obj = geom_to_visual.get(geom, None)
                                        col = [0.7, 0.7, 0.7]
                                        if visual_obj and hasattr(visual_obj, 'material') and visual_obj.material and visual_obj.material.color is not None:
                                            col = np.array(visual_obj.material.color[:3]).astype(float)
                                            if col.max() > 1.01:
                                                col /= 255.0
                                        elif "abb" in id_short.lower():
                                            col = [246.0/255.0, 120.0/255.0, 40.0/255.0]
                                            
                                        self.plotter.add_mesh(pm, name=actor_name, color=col, smooth_shading=True, specular=0.5, ambient=0.2, show_scalar_bar=False)
                                loaded_count += 1
                            except Exception as e_robot:
                                print(f"Error loading robot in layout preview: {e_robot}")
                    continue

                if not vis_data: continue

                beh_data = next((v for k,v in submodels_data.items() if "BehaviorMapping" in k or "Behavior" in k), None)
                hidden_nodes = set()
                if beh_data:
                    act_types = ["Conveyor", "RotateContinuous", "TranslateContinuous", "Piston", "Linear"]
                    sens_types = ["TriggerZone", "Presence", "Proximity"]
                    for el in beh_data.get("submodelElements", []):
                        if el["modelType"] == "SubmodelElementCollection":
                            props = {p["idShort"]: p.get("value") for p in el.get("value", []) if p["modelType"] == "Property"}
                            params = {}
                            for p in el.get("value", []):
                                if p["idShort"] == "Parameters":
                                    params = {pp["idShort"]: pp.get("value") for pp in p.get("value", []) if pp["modelType"] == "Property"}
                            b_type = props.get("Type", "")
                            node = props.get("Component", "")
                            if b_type in act_types:
                                visible = str(params.get("Visible", "True")).lower() == "true"
                                if not visible and node: hidden_nodes.add(node)
                            elif b_type in sens_types:
                                visible = str(params.get("Visible", "False")).lower() == "true"
                                if not visible and node: hidden_nodes.add(node)

                # Parse transform
                pos_val = [0.0]*3; rot_val = [0.0]*3; scale_val = [1.0]*3
                mesh_href = None
                coll_id_short = None
                for el in vis_data.get("submodelElements", []):
                    if el["modelType"] == "SubmodelElementCollection": 
                        for sel in el.get("value", []):
                            if sel["idShort"] == "Transform":
                                for t in sel.get("value", []):
                                    v = float(t.get("value", 0))
                                    if t["idShort"] == "PosX": pos_val[0] = v
                                    elif t["idShort"] == "PosY": pos_val[1] = v
                                    elif t["idShort"] == "PosZ": pos_val[2] = v
                                    elif t["idShort"] == "RotX": rot_val[0] = v
                                    elif t["idShort"] == "RotY": rot_val[1] = v
                                    elif t["idShort"] == "RotZ": rot_val[2] = v
                                    elif t["idShort"] == "ScaleX": scale_val[0] = v
                                    elif t["idShort"] == "ScaleY": scale_val[1] = v
                                    elif t["idShort"] == "ScaleZ": scale_val[2] = v
                            elif sel["idShort"] == "Mesh" and sel["modelType"] == "File":
                                mesh_href = sel.get("value")
                                coll_id_short = el["idShort"]

                if mesh_href and coll_id_short:
                    # Fetch mesh attachment
                    vis_id_node = vis_data.get("id")
                    encoded_vis_id = base64.urlsafe_b64encode(vis_id_node.encode()).decode().rstrip("=")
                    dl_url = f"http://localhost:8081/submodels/{encoded_vis_id}/submodel-elements/{coll_id_short}.Mesh/attachment"
                    m_res = requests.get(dl_url, timeout=5)
                    if m_res.status_code == 200:
                        import tempfile
                        tp = os.path.join(tempfile.gettempdir(), f"{id_short}_layout.glb")
                        with open(tp, "wb") as f: f.write(m_res.content)
                        
                        # Load geometry
                        scene = trimesh.load(tp, process=False)
                        
                        # Map GLB Y-up coordinates to Unity left-handed coordinates (reflected Z-flip)
                        pos_pv = [pos_val[0], pos_val[1], pos_val[2]]
                        rz, rx, ry = rot_val[2], -rot_val[0], -rot_val[1]
                        scale_pv = [scale_val[0], scale_val[1], scale_val[2]]
                        
                        T = tf.translation_matrix(pos_pv)
                        R = tf.euler_matrix(np.radians(rz), np.radians(rx), np.radians(ry), 'szxy')
                        S = np.diag([scale_pv[0], scale_pv[1], scale_pv[2], 1.0])
                        M = tf.concatenate_matrices(T, R, S)
                        
                        if isinstance(scene, trimesh.Scene):
                            # Gather material metadata
                            geo_mats = {}
                            for gn, mesh in scene.geometry.items():
                                tx = None; cl = None; mt = 0.0; rg = 0.5
                                if hasattr(mesh, 'visual') and hasattr(mesh.visual, 'material'):
                                    mat = mesh.visual.material; img = getattr(mat, 'image', getattr(mat, 'baseColorTexture', None))
                                    if img:
                                        try: tx = pv.Texture(np.array(img))
                                        except: pass
                                    if hasattr(mat, 'baseColorFactor') and mat.baseColorFactor is not None:
                                        cl = mat.baseColorFactor[:3]
                                    mt = getattr(mat, 'metallicFactor', 0.0)
                                    rg = getattr(mat, 'roughnessFactor', 0.5)
                                geo_mats[gn] = (tx, cl, mt, rg)
                            
                            # Recursively add nodes with graph transforms + global matrix
                            root_nodes = [node for node in scene.graph.nodes if scene.graph.transforms.parents.get(node) is None]
                            
                            def is_hidden(node):
                                curr = node
                                while curr is not None:
                                    if curr in hidden_nodes:
                                        return True
                                    curr = scene.graph.transforms.parents.get(curr)
                                return False

                            def add_preview_node(node):
                                if node in scene.graph.nodes_geometry:
                                    if not is_hidden(node):
                                        node_tf, gn = scene.graph[node]
                                        mi = scene.geometry[gn].copy()
                                        combined_tf = M @ node_tf
                                        mi.apply_transform(combined_tf)
                                        pm = pv.wrap(mi)
                                        tx, cl, mt, rg = geo_mats.get(gn, (None, None, 0.0, 0.5))
                                        self.plotter.add_mesh(pm, texture=tx, color=cl if not tx else None, smooth_shading=True, pbr=True, metallic=mt, roughness=rg, show_scalar_bar=False)
                                for c in scene.graph.transforms.children.get(node, []):
                                    add_preview_node(c)
                                    
                            for r in root_nodes:
                                add_preview_node(r)
                        elif isinstance(scene, trimesh.Trimesh):
                            pm = pv.wrap(scene)
                            pm.transform(M)
                            self.plotter.add_mesh(pm, pbr=True, show_scalar_bar=False)
                        
                        loaded_count += 1

            self.plotter.reset_camera()
            self.plotter.camera.up = (0, 1, 0)
            while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
            QMessageBox.information(self, "Success", f"Factory layout loaded! Rendered {loaded_count} active AAS resources.")
        except Exception as ex:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            traceback.print_exc()
            QMessageBox.critical(self, "Error", f"Failed to load factory layout: {ex}")
        finally:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def load_cloned_aas(self, shell_desc):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            shell_id = shell_desc.get("id")
            encoded_shell_id = base64.urlsafe_b64encode(shell_id.encode()).decode().rstrip("=")
            
            # Fetch the actual shell from the repository
            shell_res = requests.get(f"http://localhost:8081/shells/{encoded_shell_id}", timeout=5)
            if shell_res.status_code != 200: raise Exception(f"Failed to fetch Shell from repository (Code {shell_res.status_code})")
            shell_data = shell_res.json()
            
            # Extract Submodel References directly from the Shell
            submodel_refs = shell_data.get("submodels", [])
            submodels_data = {}
            for ref in submodel_refs:
                keys = ref.get("keys", [])
                if keys:
                    sm_id = keys[0].get("value")
                    encoded_sm_id = base64.urlsafe_b64encode(sm_id.encode()).decode().rstrip("=")
                    sm_res = requests.get(f"http://localhost:8081/submodels/{encoded_sm_id}", timeout=5)
                    if sm_res.status_code == 200:
                        sm_data = sm_res.json()
                        submodels_data[sm_data.get("idShort", "")] = sm_data

            # Look for submodels by parsing the actual Submodel IDs/data we just fetched
            is_urdf = "GeometryData" in submodels_data and "PositionData" in submodels_data
            
            is_logical = "ControlLogic" in submodels_data and not any(k in submodels_data for k in ["GeometryData", "VisualStructure", "BehaviorMapping"])
            
            if is_urdf:
                # It's a URDF Robot AAS
                geo_data = submodels_data["GeometryData"]
                pos_data = submodels_data["PositionData"]
                
                # 1. Reconstruct robot folder structure and download files
                import tempfile
                temp_robot_dir = tempfile.mkdtemp()
                self.current_robot_root = os.path.abspath(temp_robot_dir).replace('\\', '/')
                
                # Find the GeometryData ID to download attachments
                geo_id = geo_data.get("id")
                encoded_geo_id = base64.urlsafe_b64encode(geo_id.encode()).decode().rstrip("=")
                
                urdf_rel_path = None
                urdf_file_element = None
                
                # First scan elements to find the URDF file
                for el in geo_data.get("submodelElements", []):
                    if el["modelType"] == "File":
                        val = el.get("value", "")
                        if val.lower().endswith(".urdf") or el.get("idShort", "").lower().endswith("_urdf") or el.get("idShort", "").lower().endswith("_urdf_dot_urdf"):
                            urdf_file_element = el
                            urdf_rel_path = get_file_rel_path(el)
                                
                if not urdf_file_element:
                    raise Exception("AAS GeometryData is missing the URDF file.")
                    
                # Now download all files
                self.files_to_upload = {}
                for el in geo_data.get("submodelElements", []):
                    if el["modelType"] == "File":
                        rel_path = get_file_rel_path(el)
                        id_short = el.get("idShort")
                            
                        # Download attachment
                        dl_url = f"http://localhost:8081/submodels/{encoded_geo_id}/submodel-elements/{id_short}/attachment"
                        m_res = requests.get(dl_url, timeout=5)
                        if m_res.status_code == 200:
                            dest_path = os.path.join(temp_robot_dir, rel_path.replace("/", os.sep))
                            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                            with open(dest_path, "wb") as f:
                                f.write(m_res.content)
                            self.files_to_upload[dest_path] = rel_path
                        else:
                            print(f"[DOWNLOAD ERROR] Failed to download {id_short} (HTTP Code: {m_res.status_code})")
                            raise Exception(f"Failed to download robot mesh/file '{id_short}' (HTTP Code {m_res.status_code})")
                
                target_urdf_path = os.path.join(temp_robot_dir, urdf_rel_path.replace("/", os.sep))
                self.current_urdf_path = target_urdf_path
                self.glb_path = target_urdf_path
                self.model_type = "URDF"
                
                # Set up resolver
                self.resolver = XacroResolver(self.current_robot_root)
                
                # Load URDF XML content
                with open(target_urdf_path, 'r', encoding='utf-8') as f:
                    self.current_urdf_xml = f.read()
                self.current_urdf_tree = ET.fromstring(self.current_urdf_xml.strip())
                
                # Load robot object using urdfpy
                self.robot = URDF.load(target_urdf_path)
                
                # Basic Info
                self.edit_kit_name.setText(shell_desc.get("idShort", "ClonedRobot"))
                self.edit_aas_id.setText(shell_desc.get("idShort", "ClonedRobot"))
                
                desc = ""
                if "KitConfiguration" in submodels_data:
                    cfg = submodels_data["KitConfiguration"]
                    for el in cfg.get("submodelElements", []):
                        if el["idShort"] == "Description":
                            desc = el.get("value", "")
                self.edit_desc.setText(desc if desc else "Cloned Robot System")
                
                # Parse transform from PositionData
                for el in pos_data.get("submodelElements", []):
                    if el["modelType"] == "Property":
                        try:
                            v = float(el.get("value", 0))
                            id_s = el["idShort"]
                            if id_s == "Pos_X": self.sys_pos[0].setValue(v)
                            elif id_s == "Pos_Y": self.sys_pos[1].setValue(v)
                            elif id_s == "Pos_Z": self.sys_pos[2].setValue(v)
                            elif id_s == "Rot_X": self.sys_rot[0].setValue(v)
                            elif id_s == "Rot_Y": self.sys_rot[1].setValue(v)
                            elif id_s == "Rot_Z": self.sys_rot[2].setValue(v)
                            elif id_s == "Scale_X": self.sys_scale[0].setValue(v)
                            elif id_s == "Scale_Y": self.sys_scale[1].setValue(v)
                            elif id_s == "Scale_Z": self.sys_scale[2].setValue(v)
                        except:
                            pass
                        
                # Update label and build the 3D visualization
                self.lbl_glb_path.setText(f"URDF: {self.robot.name} ({os.path.basename(target_urdf_path)})")
                self._build_urdf_view()
                
            elif is_logical:
                # It's a purely logical AAS (e.g. FMS logical orchestrator)
                self.model_type = "GLB"
                self.glb_path = None
                self.lbl_glb_path.setText("None (Logical AAS)")
                self.plotter.clear()
                
                self.edit_kit_name.setText(shell_desc.get("idShort", "ClonedLogical"))
                self.edit_aas_id.setText(shell_desc.get("idShort", "ClonedLogical"))
                self.edit_desc.setText("Cloned Logical AAS Orchestrator")
                
                # Behavior list is empty for logical AAS
                for w in list(self.actuator_widgets): self.remove_actuator(w)
                for w in list(self.sensor_widgets): self.remove_sensor(w)

                logic_data = next((v for k,v in submodels_data.items() if "ControlLogic" in k or "Logic" in k), None)
                self.logic_rules = {"start": [], "sequences": []}
                if logic_data:
                    for el in logic_data.get("submodelElements", []):
                        if el.get("idShort") == "Rules":
                            rules_json_str = el.get("value", "")
                            if rules_json_str:
                                try:
                                    loaded_rules = json.loads(rules_json_str)
                                    if isinstance(loaded_rules, dict):
                                        self.logic_rules = loaded_rules
                                except Exception:
                                    traceback.print_exc()
                if hasattr(self, 'list_sequences'):
                    self.load_sequences()
                
            else:
                # Extract visual structure and behavior submodels
                vis_data = next((v for k,v in submodels_data.items() if "VisualStructure" in k or "Visual" in k), None)
                beh_data = next((v for k,v in submodels_data.items() if "BehaviorMapping" in k or "Behavior" in k), None)
                aid_data = next((v for k,v in submodels_data.items() if "AssetInterfacesDescription" in k), None)
                
                # Basic Info
                self.edit_kit_name.setText(shell_desc.get("idShort", "ClonedKit"))
                self.edit_aas_id.setText(shell_desc.get("idShort", "ClonedKit"))
                
                # Fetch description from configuration if available
                desc = ""
                if "KitConfiguration" in submodels_data:
                    cfg = submodels_data["KitConfiguration"]
                    for el in cfg.get("submodelElements", []):
                        if el.get("idShort") == "Description":
                            desc = el.get("value", "")
                self.edit_desc.setText(desc)

                if aid_data:
                    aid_props = {el["idShort"]: el.get("value") for el in aid_data.get("submodelElements", []) if el.get("modelType") == "Property"}
                    proto = aid_props.get("Protocol", "Sockets")
                    ip = aid_props.get("EndpointIP", "127.0.0.1")
                    port = aid_props.get("EndpointPort", 5000)
                    if hasattr(self, "combo_comm_protocol"):
                        self.combo_comm_protocol.setCurrentText(str(proto))
                    if hasattr(self, "edit_comm_ip"):
                        self.edit_comm_ip.setText(str(ip))
                    if hasattr(self, "spin_comm_port"):
                        try:
                            self.spin_comm_port.setValue(int(port))
                        except (ValueError, TypeError):
                            pass
                
                # Transform & Mesh
                mesh_href = None
                if vis_data:
                    for el in vis_data.get("submodelElements", []):
                        if el["modelType"] == "SubmodelElementCollection": 
                            for sel in el.get("value", []):
                                if sel["idShort"] == "Transform":
                                    for t in sel.get("value", []):
                                        v = float(t.get("value", 0))
                                        if t["idShort"] == "PosX": self.sys_pos[0].setValue(v)
                                        elif t["idShort"] == "PosY": self.sys_pos[1].setValue(v)
                                        elif t["idShort"] == "PosZ": self.sys_pos[2].setValue(v)
                                        elif t["idShort"] == "RotX": self.sys_rot[0].setValue(v)
                                        elif t["idShort"] == "RotY": self.sys_rot[1].setValue(v)
                                        elif t["idShort"] == "RotZ": self.sys_rot[2].setValue(v)
                                        elif t["idShort"] == "ScaleX": self.sys_scale[0].setValue(v)
                                        elif t["idShort"] == "ScaleY": self.sys_scale[1].setValue(v)
                                        elif t["idShort"] == "ScaleZ": self.sys_scale[2].setValue(v)
                                elif sel["idShort"] == "Mesh" and sel["modelType"] == "File":
                                    mesh_href = sel.get("value")
                                elif sel["idShort"] == "DynamicObject" and sel["modelType"] == "Property":
                                    is_dyn = str(sel.get("value", "False")).lower() == "true"
                                    self.check_is_dynamic.setChecked(is_dyn)
                else:
                    self.glb_path = None
                    self.lbl_glb_path.setText("None (No 3D Model)")
                    self.plotter.clear()
    
                # Behavior
                for w in list(self.actuator_widgets): self.remove_actuator(w)
                for w in list(self.sensor_widgets): self.remove_sensor(w)
                
                act_types = ["Conveyor", "RotateContinuous", "TranslateContinuous", "Piston", "Linear"]
                sens_types = ["TriggerZone", "Presence", "Proximity"]
                
                if beh_data:
                    for el in beh_data.get("submodelElements", []):
                        if el["modelType"] == "SubmodelElementCollection":
                            props = {p["idShort"]: p.get("value") for p in el.get("value", []) if p["modelType"] == "Property"}
                            params = {}
                            for p in el.get("value", []):
                                if p["idShort"] == "Parameters": params = {pp["idShort"]: pp.get("value") for pp in p.get("value", []) if pp["modelType"] == "Property"}
                                    
                            b_type = props.get("Type", ""); pin_id = props.get("PinID", ""); node = props.get("Component", "")
                            
                            if b_type in act_types:
                                self.add_actuator(); w = self.actuator_widgets[-1]
                                w.edit_pin.setText(pin_id); w.edit_node.setText(node); w.combo_behavior.setCurrentText(b_type)
                                w.spin_x.setValue(float(params.get("AxisX", 0))); w.spin_y.setValue(float(params.get("AxisY", 0))); w.spin_z.setValue(float(params.get("AxisZ", 0)))
                                w.spin_speed.setValue(float(params.get("Speed", 1.0))); w.check_visible.setChecked(str(params.get("Visible", "True")).lower() == "true")
                                if "StopSensorComponent" in params: w.combo_stop_sens.setCurrentText(params["StopSensorComponent"])
                                if "RealActuatorEnabled" in params:
                                    r_enabled = str(params.get("RealActuatorEnabled", "False")).lower() == "true" if isinstance(params.get("RealActuatorEnabled"), str) else bool(params.get("RealActuatorEnabled", False))
                                    w.check_real_actuator.setChecked(r_enabled)
                                if "RealPortID" in params:
                                    w.edit_real_port.setText(str(params.get("RealPortID", "")))
                            elif b_type in sens_types:
                                self.add_sensor(); w = self.sensor_widgets[-1]
                                w.edit_pin.setText(pin_id); w.edit_node.setText(node); w.combo_behavior.setCurrentText(b_type)
                                w.edit_filter.setText(params.get("TargetFilter", "Metal"))
                                w.check_visible.setChecked(str(params.get("Visible", "False")).lower() == "true")
                                w.check_active_low.setChecked(str(params.get("ActiveLow", "False")).lower() == "true")
                                w.check_snap.setChecked("SnapTarget" in params)
                                if hasattr(w, "check_real_sensor") and "RealSensorEnabled" in params:
                                    r_enabled = str(params.get("RealSensorEnabled", "False")).lower() == "true" if isinstance(params.get("RealSensorEnabled"), str) else bool(params.get("RealSensorEnabled", False))
                                    w.check_real_sensor.setChecked(r_enabled)
                                if hasattr(w, "edit_real_port") and "RealPortID" in params:
                                    w.edit_real_port.setText(str(params.get("RealPortID", "")))
                                if "MoveTarget" in params:
                                    w.check_move.setChecked(True)
                                    w.edit_move_target.setText(str(params.get("MoveTarget", "")))
                                    w.combo_move_state.setCurrentText("HIGH" if str(params.get("MoveOnState", "True")).lower() == "true" else "LOW")
                                    mode_val = params.get("MoveMode", "Both")
                                    mode_reverse_map = {"Both": "Position and Rotation", "Position": "Position Only", "Rotation": "Rotation Only"}
                                    w.combo_move_mode.setCurrentText(mode_reverse_map.get(mode_val, "Position and Rotation"))
                                    w.spin_mpos_x.setValue(float(params.get("MovePosX", 0)))
                                    w.spin_mpos_y.setValue(float(params.get("MovePosY", 0)))
                                    w.spin_mpos_z.setValue(float(params.get("MovePosZ", 0)))
                                    w.spin_mrot_x.setValue(float(params.get("MoveRotX", 0)))
                                    w.spin_mrot_y.setValue(float(params.get("MoveRotY", 0)))
                                    w.spin_mrot_z.setValue(float(params.get("MoveRotZ", 0)))
                                    w.spin_mdur.setValue(float(params.get("MoveDuration", 0.5)))
                    self.update_all_actuator_dropdowns()
    
                # Control Logic
                logic_data = next((v for k,v in submodels_data.items() if "ControlLogic" in k or "Logic" in k), None)
                self.logic_rules = {"start": [], "sequences": []}
                if logic_data:
                    for el in logic_data.get("submodelElements", []):
                        if el["idShort"] == "Rules":
                            rules_json_str = el.get("value", "")
                            if rules_json_str:
                                try:
                                    loaded_rules = json.loads(rules_json_str)
                                    if isinstance(loaded_rules, dict):
                                        self.logic_rules = loaded_rules
                                except Exception:
                                    traceback.print_exc()
                    if hasattr(self, 'list_sequences'):
                        self.load_sequences()
    
                # Mesh Download
                if mesh_href and vis_data:
                    vis_id = vis_data.get("id")
                    encoded_vis_id = base64.urlsafe_b64encode(vis_id.encode()).decode().rstrip("=")
                    
                    coll_id_short = None
                    for el in vis_data.get("submodelElements", []):
                        if el["modelType"] == "SubmodelElementCollection": 
                            for sel in el.get("value", []):
                                if sel["idShort"] == "Mesh":
                                    coll_id_short = el["idShort"]
                                    break
                                    
                    if coll_id_short:
                        dl_url = f"http://localhost:8081/submodels/{encoded_vis_id}/submodel-elements/{coll_id_short}.Mesh/attachment"
                        m_res = requests.get(dl_url, timeout=5)
                        if m_res.status_code == 200:
                            import tempfile
                            tp = os.path.join(tempfile.gettempdir(), f"{shell_desc['idShort']}_cloned.glb")
                            with open(tp, "wb") as f: f.write(m_res.content)
                            self.glb_path = tp; self.lbl_glb_path.setText(f"[Cloned] {shell_desc['idShort']}.glb"); self.load_model(tp)
                            
            self.stack.setCurrentIndex(1); self.update_nav()
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            QMessageBox.information(self, "Success", "AAS Cloned successfully! You can now edit and deploy as a new Kit.")
        except Exception as e:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            traceback.print_exc()
            QMessageBox.critical(self, "Error", f"Failed to clone AAS: {e}")
        finally:
            while QApplication.overrideCursor() is not None: QApplication.restoreOverrideCursor()

    def save_wizard_state(self):
        return {
            "kit_name": self.edit_kit_name.text(),
            "aas_id": self.edit_aas_id.text(),
            "description": self.edit_desc.text(),
            "dynamic": self.check_is_dynamic.isChecked(),
            "comm_protocol": self.combo_comm_protocol.currentText(),
            "comm_ip": self.edit_comm_ip.text().strip(),
            "comm_port": self.spin_comm_port.value(),
            "glb_path": self.glb_path,
            "pos": [self.sys_pos[i].value() for i in range(3)],
            "rot": [self.sys_rot[i].value() for i in range(3)],
            "scale": [self.sys_scale[i].value() for i in range(3)],
            "actuators": [w.get_data() for w in self.actuator_widgets],
            "sensors": [w.get_data() for w in self.sensor_widgets],
            "control_logic": self.logic_rules
        }

    def restore_wizard_state(self, state, config_dir=None):
        self.glb_path = state.get("glb_path")
        
        components = state.get("components", [])
        if components:
            comp = components[0]
            pos = comp.get("position", [0.0, 0.0, 0.0])
            rot = comp.get("rotation", [0.0, 0.0, 0.0])
            scale = comp.get("scale", [1.0, 1.0, 1.0])
            is_dynamic = comp.get("dynamic", state.get("dynamic", False))
            actuators = comp.get("actuators", [])
            sensors = comp.get("sensors", [])
            mesh_name = comp.get("mesh")
            if mesh_name and not self.glb_path:
                if config_dir:
                    candidate = os.path.join(config_dir, mesh_name)
                    if os.path.exists(candidate):
                        self.glb_path = candidate
                if not self.glb_path:
                    layer0_candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Layer 0 - 3D Models", mesh_name)
                    if os.path.exists(layer0_candidate):
                        self.glb_path = layer0_candidate
                if not self.glb_path and os.path.exists(mesh_name):
                    self.glb_path = os.path.abspath(mesh_name)
        else:
            pos = state.get("pos", state.get("position", [0.0, 0.0, 0.0]))
            rot = state.get("rot", state.get("rotation", [0.0, 0.0, 0.0]))
            scale = state.get("scale", [1.0, 1.0, 1.0])
            is_dynamic = state.get("dynamic", False)
            actuators = state.get("actuators", [])
            sensors = state.get("sensors", [])

        self.lbl_glb_path.setText(os.path.basename(self.glb_path) if self.glb_path else "No file selected")
        if self.glb_path and os.path.exists(self.glb_path):
            self.load_model(self.glb_path)

        self.edit_kit_name.setText(state.get("kit_name", ""))
        self.edit_aas_id.setText(state.get("aas_id", ""))
        self.edit_desc.setText(state.get("description", ""))
        self.check_is_dynamic.setChecked(is_dynamic)
        self.combo_comm_protocol.setCurrentText(state.get("comm_protocol", "None"))
        self.edit_comm_ip.setText(state.get("comm_ip", "192.168.10.1"))
        self.spin_comm_port.setValue(state.get("comm_port", 8888))

        for i in range(3):
            self.sys_pos[i].setValue(pos[i])
            self.sys_rot[i].setValue(rot[i])
            self.sys_scale[i].setValue(scale[i])
            
        for w in list(self.actuator_widgets): self.remove_actuator(w)
        for w in list(self.sensor_widgets): self.remove_sensor(w)
        
        for act in actuators:
            self.add_actuator(); w = self.actuator_widgets[-1]
            w.edit_pin.setText(act.get("pin_id", ""))
            w.edit_node.setText(act.get("node", ""))
            w.combo_behavior.setCurrentText(act.get("behavior", ""))
            axis = act.get("axis", [0, 0, 0])
            w.spin_x.setValue(axis[0])
            w.spin_y.setValue(axis[1])
            w.spin_z.setValue(axis[2])
            w.spin_speed.setValue(act.get("speed", 1.0))
            w.check_visible.setChecked(act.get("visible", True))
            w.check_real_actuator.setChecked(act.get("real_actuator_enabled", False))
            w.edit_real_port.setText(act.get("real_port_id", "R0_0"))
            
        for sns in sensors:
            self.add_sensor(); w = self.sensor_widgets[-1]
            w.edit_pin.setText(sns.get("pin_id", ""))
            w.edit_node.setText(sns.get("node", ""))
            w.combo_behavior.setCurrentText(sns.get("behavior", ""))
            w.edit_filter.setText(sns.get("target_filter", "Metal"))
            w.check_visible.setChecked(sns.get("visible", False))
            w.check_active_low.setChecked(sns.get("active_low", False))
            w.check_snap.setChecked(sns.get("snap_product", False))
            w.check_real_sensor.setChecked(sns.get("real_sensor_enabled", False))
            w.edit_real_port.setText(sns.get("real_port_id", "I0_0"))
            
        self.update_all_actuator_dropdowns()

        for i, act in enumerate(actuators):
            if i < len(self.actuator_widgets) and "stop_sensor" in act:
                self.actuator_widgets[i].combo_stop_sens.setCurrentText(act["stop_sensor"])

        # Restore logic rules
        self.logic_rules = state.get("control_logic", {"start": [], "sequences": []})
        if hasattr(self, 'list_sequences'):
            self.list_sequences.blockSignals(True)
            self.list_sequences.clear()
            self.list_sequences.addItem("Start Sequence")
            for seq in self.logic_rules.get("sequences", []):
                self.list_sequences.addItem(seq.get("name", "Custom Sequence"))
            self.list_sequences.setCurrentRow(0)
            self.list_sequences.blockSignals(False)
            self.on_sequence_selected(0)

    def reset_wizard(self):
        # 1. Clear GLB model path
        self.glb_path = None
        self.lbl_glb_path.setText("No file selected")
        
        # 2. Reset text fields
        self.edit_kit_name.setText("ConveyorKit")
        self.edit_aas_id.setText("Conveyor_01")
        self.edit_desc.setText("Standard Conveyor System")
        self.check_is_dynamic.setChecked(False)
        
        # 3. Reset transforms
        for i in range(3):
            self.sys_pos[i].setValue(0.0)
            self.sys_rot[i].setValue(0.0)
            self.sys_scale[i].setValue(1.0)
            
        # 4. Remove all actuators and sensors
        for w in list(self.actuator_widgets):
            self.remove_actuator(w)
        for w in list(self.sensor_widgets):
            self.remove_sensor(w)
            
        # 5. Clear plotter and node tree
        self.plotter.clear()
        self.node_tree.clear()
        self.meshes = {}
        self.actors = {}
        
        # Reset control logic
        self.logic_rules = {"start": [], "sequences": []}

        # 6. Update UI
        self.update_validation_ui()

    def delete_aas_by_id(self, shell_id, submodel_ids=None):
        import urllib.parse
        import base64
        
        def get_all_encodings(raw_id):
            encodings = []
            if not raw_id:
                return encodings
            # 1. Base64 URL-safe (padded and unpadded)
            b64_url = base64.urlsafe_b64encode(raw_id.encode()).decode()
            encodings.append(b64_url)
            encodings.append(b64_url.rstrip("="))
            # 2. Standard Base64 (padded and unpadded)
            b64_std = base64.b64encode(raw_id.encode()).decode()
            encodings.append(b64_std)
            encodings.append(b64_std.rstrip("="))
            # 3. URL Encoded
            encodings.append(urllib.parse.quote_plus(raw_id))
            encodings.append(urllib.parse.quote(raw_id))
            return list(set(encodings))

        def try_delete_all_variants(base_url, raw_id):
            for enc in get_all_encodings(raw_id):
                url = f"{base_url}/{enc}"
                try:
                    r = requests.delete(url, timeout=5)
                    if r.status_code in [200, 204]:
                        print(f"Successfully deleted {url}")
                except Exception as e:
                    print(f"Delete failed for {url}: {e}")

        # Fetch shell to get submodel references before we delete it
        found_submodels = []
        for enc_shell in get_all_encodings(shell_id):
            try:
                shell_res = requests.get(f"http://localhost:8081/shells/{enc_shell}", timeout=5)
                if shell_res.status_code == 200:
                    shell_data = shell_res.json()
                    submodel_refs = shell_data.get("submodels", [])
                    for ref in submodel_refs:
                        keys = ref.get("keys", [])
                        if keys:
                            sm_id = keys[0].get("value")
                            if sm_id and sm_id not in found_submodels:
                                found_submodels.append(sm_id)
                    break
            except Exception as e:
                print(f"Could not retrieve shell references for encoding {enc_shell}: {e}")

        # Delete submodels found in the shell
        for sm_id in found_submodels:
            try_delete_all_variants("http://localhost:8081/submodels", sm_id)
            try_delete_all_variants("http://localhost:8082/submodel-descriptors", sm_id)
            try_delete_all_variants("http://localhost:8083/submodel-descriptors", sm_id)

        # Explicitly delete provided submodels
        if submodel_ids:
            for sm_id in submodel_ids:
                try_delete_all_variants("http://localhost:8081/submodels", sm_id)
                try_delete_all_variants("http://localhost:8082/submodel-descriptors", sm_id)
                try_delete_all_variants("http://localhost:8083/submodel-descriptors", sm_id)

        # Delete shell from repository and registry
        try_delete_all_variants("http://localhost:8081/shells", shell_id)
        try_delete_all_variants("http://localhost:8082/shell-descriptors", shell_id)

    def update_validation_ui(self):
        valid_aas = is_valid_aas_id(self.edit_aas_id.text())
        self.edit_aas_id.setStyleSheet(VALID_STYLE if valid_aas else ERROR_STYLE)
        overall_valid = valid_aas
        for w in self.actuator_widgets + self.sensor_widgets:
            if not is_valid_aas_id(w.edit_pin.text()): overall_valid = False; break
        self.lbl_global_error.setVisible(not overall_valid)
        self.btn_next.setEnabled(overall_valid)

    def add_actuator(self):
        w = ActuatorWidget(self); self.actuator_widgets.append(w); self.act_layout.addWidget(w); w.activate()
    def remove_actuator(self, w):
        w.hide_visuals()
        self.actuator_widgets.remove(w); w.deleteLater(); self.current_config_widget = None; self.update_validation_ui()
    def add_sensor(self):
        w = SensorWidget(self); self.sensor_widgets.append(w); self.sens_layout.addWidget(w); self.update_all_actuator_dropdowns(); w.activate()
    def remove_sensor(self, w):
        self.sensor_widgets.remove(w); w.deleteLater(); self.current_config_widget = None; self.update_all_actuator_dropdowns(); self.update_validation_ui()

    def update_all_actuator_dropdowns(self):
        for aw in self.actuator_widgets: aw.update_stop_sensors()

    def on_mesh_picked(self, mesh):
        for n, m in self.meshes.items():
            if m == mesh:
                self.select_node(n)
                target_w = self.current_config_widget
                if target_w is None and self.stack.currentIndex() == 3 and self.actuator_widgets:
                    target_w = self.actuator_widgets[0]
                    target_w.activate()
                elif target_w is None and self.stack.currentIndex() == 2 and self.sensor_widgets:
                    target_w = self.sensor_widgets[0]
                    target_w.activate()
                if target_w: target_w.set_node(n)
                break

    def on_tree_item_clicked(self, it, col):
        name = it.text(0)
        self.select_node(name)
        target_w = self.current_config_widget
        if target_w is None and self.stack.currentIndex() == 3 and self.actuator_widgets:
            target_w = self.actuator_widgets[0]
            target_w.activate()
        elif target_w is None and self.stack.currentIndex() == 2 and self.sensor_widgets:
            target_w = self.sensor_widgets[0]
            target_w.activate()
        if target_w: target_w.set_node(name)

    def select_node(self, n):
        family = self.get_node_family(n)
        for name, actor in self.actors.items(): actor.prop.opacity = 1.0 if name in family else 0.15

    def get_node_family(self, name):
        f = {name}
        if self.model_type == "URDF" and self.robot:
            link_children = {}
            for j in self.robot.joints:
                if j.parent not in link_children:
                    link_children[j.parent] = []
                link_children[j.parent].append((j, j.child))
            
            visited = set()
            def collect(n):
                if n in visited: return
                visited.add(n)
                f.add(n)
                if n in link_children:
                    for joint, child in link_children[n]:
                        collect(joint.name)
                        collect(child)
            collect(name)
        elif hasattr(self, 'scene_graph'):
            for c in self.scene_graph.transforms.children.get(name, []): f.update(self.get_node_family(c))
        return f
    def browse_glb(self):
        default_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Layer 0 - 3D Models")
        initial_path = default_dir if os.path.exists(default_dir) else ""
        p, _ = QFileDialog.getOpenFileName(self, "Select GLB", initial_path, "GLB Files (*.glb)")
        if p: 
            self.glb_path = p; self.lbl_glb_path.setText(os.path.basename(p)); self.load_model(p)
            
    def browse_urdf(self):
        path = QFileDialog.getExistingDirectory(self, "Select URDF / Xacro Folder")
        if not path:
            return
        self.current_robot_root = os.path.abspath(path).replace('\\', '/')
        self.resolver = XacroResolver(self.current_robot_root)
        
        candidates = []
        for full_path in self.resolver.file_map.values():
            if full_path.startswith(self.current_robot_root):
                if full_path.lower().endswith('.urdf') or full_path.lower().endswith('.urdf.xacro'):
                    candidates.append(full_path)
        candidates = sorted(list(set(candidates)))
        
        if not candidates: 
            return QMessageBox.warning(self, "Error", "No URDF/Xacro found in the selected directory!")
        
        target = candidates[0]
        if len(candidates) > 1:
            item, ok = QInputDialog.getItem(self, "Choose Entry URDF/Xacro", "Primary File?", [os.path.basename(c) for c in candidates], 0, False)
            if ok:
                target = [c for c in candidates if os.path.basename(c) == item][0]
            else:
                return
        
        try:
            mappings = {"ur_type": "ur3e", "name": "ur"} # Defaults
            
            if target.lower().endswith('.xacro'):
                print("[LOAD] Verifying dependencies...")
                while True:
                    missing = DependencyManager.check_recursive_xacro(target, self.resolver)
                    if missing:
                        print(f"   [LOAD] Found {len(missing)} missing includes.")
                        if DependencyDialog(missing, self.resolver, self).exec() == QDialog.DialogCode.Accepted:
                            continue # Check again after adding folder
                        else:
                            return # User cancelled
                    break
                
                with open(target, 'r', encoding='utf-8') as f:
                    content = f.read()
                    is_pure_macro = "<xacro:macro" in content and "<link" not in content and "<joint" not in content
                    if is_pure_macro and not re.search(r'<xacro:[^m][^a][^c][^r][^o]', content):
                        print("[LOAD] WARNING: Selected file appears to be a Macro library, not a Robot assembly.")
                        res = QMessageBox.question(self, "Macro File Detected", 
                            "This file seems to contain only Macro definitions, not a full Robot.\n\n"
                            "Are you sure you want to proceed? (It might result in an empty visualization)",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                        if res == QMessageBox.StandardButton.No:
                            return

                print("[LOAD] Scanning for used Xacro arguments...")
                args = DependencyManager.scan_xacro_args(target, self.resolver)
                if args:
                    dlg = XacroArgsDialog(args, self)
                    if dlg.exec() == QDialog.DialogCode.Accepted:
                        mappings.update(dlg.results)
                    else:
                        return
                
                print("[LOAD] Expanding Xacro...")
                xml_text = expand_xacro_final(target, mappings, self.resolver)
            else:
                with open(target, 'r', encoding='utf-8') as f:
                    xml_text = f.read()

            self.current_urdf_xml = xml_text
            self.current_urdf_path = target
            root = ET.fromstring(xml_text.strip())
            
            # URDF Post-processing
            if 'name' not in root.attrib:
                root.set('name', os.path.basename(target).split('.')[0])
            for trans in root.findall(".//transmission"):
                root.remove(trans)
            
            print("[LOAD] Resolving mesh paths in URDF...")
            for m_tag in root.findall(".//mesh"):
                fn = m_tag.get("filename")
                real = self.resolver.resolve(fn)
                if real:
                    m_tag.set("filename", real)
            
            temp = os.path.join(self.current_robot_root, "_compiled.urdf")
            with open(temp, "w", encoding='utf-8') as f:
                f.write(ET.tostring(root, encoding='unicode'))
            
            self.current_urdf_tree = root
            
            try:
                print("[LOAD] Parsing final URDF...")
                self.robot = URDF.load(temp)
            except ValueError as ve:
                if "Link" in str(ve):
                    QMessageBox.warning(self, "Empty Robot", 
                        "The resulting URDF contains no Links.\n\n"
                        "Please verify if you selected the correct entry file (e.g., 'ur.urdf.xacro' instead of 'ur_macro.xacro').")
                else:
                    QMessageBox.warning(self, "Load Failed", f"ValueError loading robot:\n{str(ve)}")
                return
            except Exception as e:
                QMessageBox.critical(self, "Load Failed", f"Critical error while loading robot:\n{str(e)}")
                traceback.print_exc()
                return

            self.model_type = "URDF"
            self.glb_path = temp
            self.lbl_glb_path.setText(f"URDF: {self.robot.name} ({os.path.basename(target)})")
            
            # Compile files list to upload
            manager = AssetManager(self.current_robot_root)
            for elem in root.iter():
                for attr, val in elem.attrib.items():
                    if val and any(ext in val.lower() for ext in manager.exts):
                        manager.collect(val, self.resolver)
            self.files_to_upload = manager.files_to_upload
            
            # Rebuild PyVista visualization
            self._build_urdf_view()
            
        except Exception as e:
            print(f"[LOAD] ERROR: {e}")
            traceback.print_exc()
            QMessageBox.critical(self, "Load Failed", f"Critical error while loading robot:\n{str(e)}")

    def _build_urdf_view(self):
        print("[VIEW] Building URDF 3D visualization...")
        if hasattr(self, 'check_preview_layout'):
            self.check_preview_layout.setChecked(False)
        self.plotter.clear()
        self.meshes = {}
        self.actors = {}
        self.node_tree.clear()
        self.plotter.camera.up = (0, 0, 1)
        self.plotter.add_axes(color='white')
        self.plotter.enable_eye_dome_lighting()
        self.plotter.enable_shadows()
        self.kill_overlays()
        
        try:
            fk = self.robot.visual_geometry_fk()
        except Exception as e:
            print(f"[VIEW] Error calculating FK: {e}")
            return

        geom_to_link = {}
        geom_to_visual = {}
        for l in self.robot.links:
            for v in l.visuals:
                if v.geometry:
                    geom_to_link[v.geometry] = l.name
                    geom_to_visual[v.geometry] = v

        link_to_trimeshes = {}
        link_to_colors = {}

        for geom, transform in fk.items():
            link_name = geom_to_link.get(geom, "unnamed")
            visual_obj = geom_to_visual.get(geom, None)
            g_type = type(geom).__name__
            
            meshes = None
            try:
                meshes = geom.meshes
            except Exception as e:
                print(f"[VIEW] Warning: urdfpy failed to provide meshes for {g_type} ({link_name}): {e}")

            if meshes is None or len(meshes) == 0:
                # Primitives fallback
                if g_type == 'Cylinder':
                    try:
                        meshes = [trimesh.creation.cylinder(radius=geom.radius, height=geom.length)]
                    except Exception as e:
                        print(f"[VIEW] Cylinder fallback failed: {e}")
                elif g_type == 'Box':
                    try:
                        meshes = [trimesh.creation.box(extents=geom.size)]
                    except Exception as e:
                        print(f"[VIEW] Box fallback failed: {e}")
                elif g_type == 'Sphere':
                    try:
                        meshes = [trimesh.creation.sphere(radius=geom.radius)]
                    except Exception as e:
                        print(f"[VIEW] Sphere fallback failed: {e}")
                elif g_type == 'Mesh':
                    try:
                        loaded = trimesh.load(geom.filename)
                        if isinstance(loaded, trimesh.Scene):
                            m = loaded.dump(concatenate=True)
                        else:
                            m = loaded
                        if hasattr(geom, 'scale') and geom.scale is not None:
                            m.apply_scale(geom.scale)
                        meshes = [m]
                    except Exception as e:
                        print(f"[VIEW] Mesh fallback failed: {e}")

            if meshes is None or len(meshes) == 0:
                continue

            col = [0.7, 0.7, 0.7]
            if visual_obj and hasattr(visual_obj, 'material') and visual_obj.material and visual_obj.material.color is not None:
                col = np.array(visual_obj.material.color[:3]).astype(float)
                if col.max() > 1.01:
                    col /= 255.0

            scale_matrix = np.eye(4)
            if hasattr(geom, 'scale') and geom.scale is not None:
                s = geom.scale
                scale_matrix = np.diag([s[0], s[1], s[2], 1.0])

            total_transform = transform @ scale_matrix
            for m in meshes:
                if m is None:
                    continue
                extents = m.extents
                if np.allclose(scale_matrix[:3, :3], np.eye(3)) and np.any(extents > 10.0):
                    m.apply_scale(0.001)
                
                m_copy = m.copy()
                m_copy.apply_transform(total_transform)
                
                if link_name not in link_to_trimeshes:
                    link_to_trimeshes[link_name] = []
                    link_to_colors[link_name] = []
                link_to_trimeshes[link_name].append(m_copy)
                link_to_colors[link_name].append(col)

        for link_name, meshes_list in link_to_trimeshes.items():
            if not meshes_list:
                continue
            
            if len(meshes_list) > 1:
                try:
                    combined_trimesh = trimesh.util.concatenate(meshes_list)
                except Exception as e:
                    print(f"[VIEW] Concatenating trimeshes failed for {link_name}: {e}")
                    combined_trimesh = meshes_list[0]
            else:
                combined_trimesh = meshes_list[0]
            
            faces = np.column_stack([np.full(len(combined_trimesh.faces), 3), combined_trimesh.faces]).astype(np.int32).flatten()
            pv_mesh = pv.PolyData(combined_trimesh.vertices, faces)
            col = link_to_colors[link_name][0]
            
            actor = self.plotter.add_mesh(pv_mesh, color=col, smooth_shading=True, specular=0.5, ambient=0.2)
            self.meshes[link_name] = pv_mesh
            self.actors[link_name] = actor

        # Camera setup
        bounds = self.plotter.renderer.ComputeVisiblePropBounds()
        if all(np.isfinite(b) for b in bounds):
            center = [
                (bounds[0] + bounds[1]) / 2.0,
                (bounds[2] + bounds[3]) / 2.0,
                (bounds[4] + bounds[5]) / 2.0
            ]
            self.plotter.camera.focal_point = center
            size_x = bounds[1] - bounds[0]
            size_y = bounds[3] - bounds[2]
            size_z = bounds[5] - bounds[4]
            max_dim = max(size_x, size_y, size_z, 1.0)
            self.plotter.camera.position = [
                center[0] + max_dim * 1.5,
                center[1] + max_dim * 1.5,
                center[2] + max_dim * 1.0
            ]
        else:
            base_pos = np.array([0.0, 0.0, 0.0])
            self.plotter.camera.focal_point = base_pos
            self.plotter.camera.position = base_pos + np.array([3.0, 3.1, 2.0]) 
        self.plotter.camera.up = (0, 0, 1)
        self.plotter.camera.reset_clipping_range()
        self.plotter.render()

        # Build QTreeWidget
        link_children = {}
        for j in self.robot.joints:
            if j.parent not in link_children:
                link_children[j.parent] = []
            link_children[j.parent].append((j, j.child))
            
        def add_urdf_tree_node(link_name, parent_item):
            link_item = QTreeWidgetItem(parent_item or self.node_tree, [link_name])
            link_item.setForeground(0, QColor("#007acc"))
            
            if link_name in link_children:
                for joint, child_link_name in link_children[link_name]:
                    joint_item = QTreeWidgetItem(link_item, [joint.name])
                    joint_item.setForeground(0, QColor("#e67e22"))
                    add_urdf_tree_node(child_link_name, joint_item)
                    
            if parent_item is None:
                link_item.setExpanded(True)

        if self.robot.base_link:
            add_urdf_tree_node(self.robot.base_link.name, None)
        self.update_system_transform()
            
    def load_model(self, path):
        if hasattr(self, 'check_preview_layout'):
            self.check_preview_layout.setChecked(False)
        self.plotter.clear(); self.meshes = {}; self.actors = {}; self.node_tree.clear(); self.plotter.camera.up = (0, 1, 0)
        self.plotter.enable_eye_dome_lighting(); self.plotter.enable_shadows()
        self.kill_overlays()
        try:
            scene = trimesh.load(path, process=False)
            if isinstance(scene, trimesh.Scene):
                self.scene_graph = scene.graph; geo_mats = {}
                for gn, mesh in scene.geometry.items():
                    tx = None; cl = None; mt = 0.0; rg = 0.5
                    if hasattr(mesh, 'visual') and hasattr(mesh.visual, 'material'):
                        mat = mesh.visual.material; img = getattr(mat, 'image', getattr(mat, 'baseColorTexture', None))
                        if img:
                            try: tx = pv.Texture(np.array(img))
                            except: pass
                        if hasattr(mat, 'baseColorFactor') and mat.baseColorFactor is not None: cl = mat.baseColorFactor[:3]
                        mt = getattr(mat, 'metallicFactor', 0.0); rg = getattr(mat, 'roughnessFactor', 0.5)
                    geo_mats[gn] = (tx, cl, mt, rg)
                root_nodes = [n for n in scene.graph.nodes if scene.graph.transforms.parents.get(n) is None]
                for r in root_nodes: self.add_tree_node(r, None, scene, geo_mats)
            else:
                pm = pv.wrap(scene); self.meshes["Main"] = pm; self.actors["Main"] = self.plotter.add_mesh(pm, name="Main", pbr=True); QTreeWidgetItem(self.node_tree, ["Main"])
            # Camera setup for GLB/glTF
            bounds = self.plotter.renderer.ComputeVisiblePropBounds()
            if all(np.isfinite(b) for b in bounds):
                center = [
                    (bounds[0] + bounds[1]) / 2.0,
                    (bounds[2] + bounds[3]) / 2.0,
                    (bounds[4] + bounds[5]) / 2.0
                ]
                self.plotter.camera.focal_point = center
                size_x = bounds[1] - bounds[0]
                size_y = bounds[3] - bounds[2]
                size_z = bounds[5] - bounds[4]
                max_dim = max(size_x, size_y, size_z, 1.0)
                self.plotter.camera.position = [
                    center[0] + max_dim * 1.5,
                    center[1] + max_dim * 1.5,
                    center[2] + max_dim * 1.0
                ]
            else:
                self.plotter.reset_camera()
            self.plotter.add_axes()
            self.update_system_transform()
        except Exception as e: QMessageBox.critical(self, "Error", str(e)); traceback.print_exc()

    def add_tree_node(self, n, pi, scene, gm):
        it = QTreeWidgetItem(pi or self.node_tree, [n])
        if n in scene.graph.nodes_geometry:
            tf, gn = scene.graph[n]; mi = scene.geometry[gn].copy(); mi.apply_transform(tf); pm = pv.wrap(mi); self.meshes[n] = pm
            tx, cl, mt, rg = gm.get(gn, (None, None, 0.0, 0.5))
            ac = self.plotter.add_mesh(pm, name=n, texture=tx, color=cl if not tx else None, smooth_shading=True, pbr=True, metallic=mt, roughness=rg)
            self.actors[n] = ac; it.setForeground(0, QColor("#007acc"))
        for c in scene.graph.transforms.children.get(n, []): self.add_tree_node(c, it, scene, gm)
        if pi is None: it.setExpanded(True)

    def update_system_transform(self):
        if not hasattr(self, 'actors') or not self.actors:
            return
        
        pos = [s.value() for s in self.sys_pos]
        rot = [s.value() for s in self.sys_rot]
        scale = [s.value() for s in self.sys_scale]
        
        import trimesh.transformations as tf
        import numpy as np
        
        # Check if layout preview is active (meaning we are viewing it in Unity coordinate space)
        is_preview = hasattr(self, 'check_preview_layout') and self.check_preview_layout.isChecked()
        
        if is_preview:
            if self.model_type == "URDF":
                # Map URDF Z-up coordinates to Unity Y-up coordinates (reflected Z-flip)
                pos_pv = [-pos[1], pos[2], -pos[0]]
                # Unity rotation: rx_u = -rot[1], ry_u = rot[2], rz_u = rot[0]
                # Reflected Z-flip: rz = rz_u = rot[0], rx = -rx_u = rot[1], ry = -ry_u = -rot[2]
                rz, rx, ry = rot[0], rot[1], 90.0 - rot[2]
                scale_pv = [scale[1], scale[2], scale[0]]
            else:
                # Map GLB Y-up coordinates to Unity left-handed coordinates (reflected Z-flip)
                pos_pv = [pos[0], pos[1], pos[2]]
                # Unity rotation: rx_u = rot[0], ry_u = rot[1], rz_u = rot[2]
                # Reflected Z-flip: rz = rz_u = rot[2], rx = -rx_u = -rot[0], ry = -ry_u = -rot[1]
                rz, rx, ry = rot[2], -rot[0], -rot[1]
                scale_pv = [scale[0], scale[1], scale[2]]
                
            T = tf.translation_matrix(pos_pv)
            # Use 'szxy' static Euler matrix which evaluates Ry * Rx * Rz matching Unity's ZXY rotation sequence
            R = tf.euler_matrix(np.radians(rz), np.radians(rx), np.radians(ry), 'szxy')
            S = np.diag([scale_pv[0], scale_pv[1], scale_pv[2], 1.0])
            M = tf.concatenate_matrices(T, R, S)
            
            # Apply Z-to-Y conversion rotation (-90 degrees around X) first, so the model stands upright in Unity
            R_z_to_y = tf.euler_matrix(np.radians(-90), 0, 0, 'sxyz')
            M = M @ R_z_to_y
        else:
            T = tf.translation_matrix(pos)
            R = tf.euler_matrix(np.radians(rot[0]), np.radians(rot[1]), np.radians(rot[2]), 'sxyz')
            S = np.diag([scale[0], scale[1], scale[2], 1.0])
            M = tf.concatenate_matrices(T, R, S)
            
        self.current_system_matrix = M
        for name, actor in self.actors.items():
            if name.startswith("layout_"):
                continue
            actor.user_matrix = M
            
        for w in getattr(self, 'actuator_widgets', []):
            if getattr(w, 'arrow', None):
                try:
                    w.arrow.user_matrix = M
                except Exception:
                    pass
            
        # Center camera on the newly positioned object bounds if NOT in global layout preview
        if not is_preview:
            bounds = self.plotter.renderer.ComputeVisiblePropBounds()
            if all(np.isfinite(b) for b in bounds):
                center = [
                    (bounds[0] + bounds[1]) / 2.0,
                    (bounds[2] + bounds[3]) / 2.0,
                    (bounds[4] + bounds[5]) / 2.0
                ]
                self.plotter.camera.focal_point = center
                size_x = bounds[1] - bounds[0]
                size_y = bounds[3] - bounds[2]
                size_z = bounds[5] - bounds[4]
                max_dim = max(size_x, size_y, size_z, 1.0)
                
                # Maintain a relative distance and angle to center
                self.plotter.camera.position = [
                    center[0] + max_dim * 1.5,
                    center[1] + max_dim * 1.5,
                    center[2] + max_dim * 1.0
                ]
        self.plotter.render()

    def toggle_layout_preview(self, state):
        if state == 2 or state == True:  # Checked (Qt.CheckState.Checked is 2)
            self.load_layout_preview()
        else:
            self.unload_layout_preview()

    def unload_layout_preview(self):
        if hasattr(self, 'layout_actor_names') and self.layout_actor_names:
            for name in self.layout_actor_names:
                try:
                    self.plotter.remove_actor(name)
                except:
                    pass
            self.layout_actor_names = []
        if self.model_type == "URDF":
            self.plotter.camera.up = (0, 0, 1)
        else:
            self.plotter.camera.up = (0, 1, 0)
        self.update_system_transform()
        self.plotter.render()

    def load_layout_preview(self):
        self.plotter.camera.up = (0, 1, 0)  # Align camera to Unity's Y-up orientation
        try:
            res = requests.get("http://localhost:8082/shell-descriptors", timeout=3)
            shells = res.json().get("result", [])
            if not shells:
                QMessageBox.information(self, "Info", "No other AAS found on server.")
                return
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not connect to registry: {e}")
            if hasattr(self, 'check_preview_layout'):
                self.check_preview_layout.setChecked(False)
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        if not hasattr(self, 'layout_actor_names'):
            self.layout_actor_names = []
            
        # Clean up any existing layout actors first
        self.unload_layout_preview()

        import trimesh.transformations as tf
        import numpy as np
        loaded_count = 0

        # Exclude current AAS being edited
        current_aas_id = self.edit_aas_id.text().strip()

        try:
            for s in shells:
                shell_id = s.get("id")
                id_short = s.get("idShort", "AAS")
                
                # Exclude the current model if it's already deployed, to avoid duplicates
                if id_short == current_aas_id or shell_id == current_aas_id:
                    continue
                    
                # Fetch submodels for this shell
                encoded_shell_id = base64.urlsafe_b64encode(shell_id.encode()).decode().rstrip("=")
                res_s = requests.get(f"http://localhost:8081/shells/{encoded_shell_id}", timeout=3)
                if res_s.status_code == 200:
                    submodel_refs = res_s.json().get("submodels", [])
                    submodels_data = {}
                    for ref in submodel_refs:
                        keys = ref.get("keys", [])
                        if keys:
                            sm_id = keys[0].get("value")
                            encoded_sm_id = base64.urlsafe_b64encode(sm_id.encode()).decode().rstrip("=")
                            res_sm = requests.get(f"http://localhost:8081/submodels/{encoded_sm_id}", timeout=3)
                            if res_sm.status_code == 200:
                                sm_data = res_sm.json()
                                submodels_data[sm_data.get("idShort", "")] = sm_data
                                
                    vis_data = next((v for k,v in submodels_data.items() if "VisualStructure" in k or "Visual" in k), None)
                    
                    # Also support reading position data from PositionData if it is a robot (URDF)
                    # Robots also have their own position in the layout!
                    if "PositionData" in submodels_data and not vis_data:
                        pos_data = submodels_data["PositionData"]
                        # Reconstruct positions
                        pos_val = [0.0]*3; rot_val = [0.0]*3; scale_val = [1.0]*3
                        for el in pos_data.get("submodelElements", []):
                            if el["modelType"] == "Property":
                                try:
                                    v = float(el.get("value", 0))
                                    id_s = el["idShort"]
                                    if id_s == "Pos_X": pos_val[0] = v
                                    elif id_s == "Pos_Y": pos_val[1] = v
                                    elif id_s == "Pos_Z": pos_val[2] = v
                                    elif id_s == "Rot_X": rot_val[0] = v
                                    elif id_s == "Rot_Y": rot_val[1] = v
                                    elif id_s == "Rot_Z": rot_val[2] = v
                                    elif id_s == "Scale_X": scale_val[0] = v
                                    elif id_s == "Scale_Y": scale_val[1] = v
                                    elif id_s == "Scale_Z": scale_val[2] = v
                                except:
                                    pass
                        # Load URDF geometry if GeometryData is present
                        if "GeometryData" in submodels_data:
                            geo_data = submodels_data["GeometryData"]
                            geo_id = geo_data.get("id")
                            encoded_geo_id = base64.urlsafe_b64encode(geo_id.encode()).decode().rstrip("=")
                            
                            # Reconstruct robot in temp folder
                            import tempfile
                            temp_robot_dir = tempfile.mkdtemp()
                            
                            # Find URDF
                            urdf_rel_path = None
                            urdf_file_element = None
                            for el in geo_data.get("submodelElements", []):
                                if el["modelType"] == "File":
                                    val = el.get("value", "")
                                    if val.lower().endswith(".urdf") or el.get("idShort", "").lower().endswith("_urdf") or el.get("idShort", "").lower().endswith("_urdf_dot_urdf"):
                                        urdf_file_element = el
                                        urdf_rel_path = get_file_rel_path(el)
                                            
                            if urdf_file_element and urdf_rel_path:
                                # Download all files
                                for el in geo_data.get("submodelElements", []):
                                    if el["modelType"] == "File":
                                        val = el.get("value", "")
                                        id_short_f = el.get("idShort")
                                        rel_path = get_file_rel_path(el)
                                            
                                        dl_url = f"http://localhost:8081/submodels/{encoded_geo_id}/submodel-elements/{id_short_f}/attachment"
                                        m_res = requests.get(dl_url, timeout=5)
                                        if m_res.status_code == 200:
                                            dest_path = os.path.join(temp_robot_dir, rel_path.replace("/", os.sep))
                                            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                            with open(dest_path, "wb") as f:
                                                f.write(m_res.content)
                                        else:
                                            print(f"[DOWNLOAD ERROR] Failed to download {id_short_f} (HTTP Code: {m_res.status_code})")
                                            raise Exception(f"Failed to download robot mesh/file '{id_short_f}' (HTTP Code {m_res.status_code})")
                                                
                                target_urdf = os.path.join(temp_robot_dir, urdf_rel_path.replace("/", os.sep))
                                try:
                                    # Load robot
                                    robot_obj = URDF.load(target_urdf)
                                    # Convert to trimesh/pyvista using forward kinematics
                                    fk = robot_obj.visual_geometry_fk()
                                    
                                    geom_to_visual = {}
                                    for l in robot_obj.links:
                                        for v in l.visuals:
                                            if v.geometry:
                                                geom_to_visual[v.geometry] = v
                                    
                                    # We apply the layout position/rotation/scale *after* applying the Z-to-Y conversion rotation
                                    # since URDF robot is natively Z-up and the layout is Y-up!
                                    # Map URDF Z-up coordinates to Unity Y-up coordinates (reflected Z-flip)
                                    pos_pv = [-pos_val[1], pos_val[2], -pos_val[0]]
                                    rz, rx, ry = rot_val[0], rot_val[1], 90.0 - rot_val[2]
                                    scale_pv = [scale_val[1], scale_val[2], scale_val[0]]
                                    
                                    T_l = tf.translation_matrix(pos_pv)
                                    R_l = tf.euler_matrix(np.radians(rz), np.radians(rx), np.radians(ry), 'szxy')
                                    S_l = np.diag([scale_pv[0], scale_pv[1], scale_pv[2], 1.0])
                                    R_z_to_y = tf.euler_matrix(np.radians(-90), 0, 0, 'sxyz')
                                    M_layout = tf.concatenate_matrices(T_l, R_l, S_l) @ R_z_to_y
                                    
                                    for geom, geom_tf in fk.items():
                                        # Get meshes from urdfpy geometry
                                        meshes = getattr(geom, "meshes", None)
                                        if meshes is None or len(meshes) == 0:
                                            # Try fallback for Mesh type
                                            if type(geom).__name__ == "Mesh" and getattr(geom, "filename", None):
                                                try:
                                                    loaded = trimesh.load(geom.filename)
                                                    if isinstance(loaded, trimesh.Scene):
                                                         m = loaded.dump(concatenate=True)
                                                    else:
                                                         m = loaded
                                                    if hasattr(geom, 'scale') and geom.scale is not None:
                                                        m.apply_scale(geom.scale)
                                                    meshes = [m]
                                                except Exception as e_fallback:
                                                    print(f"Fallback mesh load failed: {e_fallback}")
                                                    
                                        if meshes is None or len(meshes) == 0:
                                            continue
                                            
                                        # Apply transform & render
                                        scale_matrix = np.eye(4)
                                        if hasattr(geom, 'scale') and geom.scale is not None:
                                            s = geom.scale
                                            scale_matrix = np.diag([s[0], s[1], s[2], 1.0])
                                        
                                        total_transform = M_layout @ geom_tf @ scale_matrix
                                        
                                        for m in meshes:
                                            if m is None: continue
                                            m_copy = m.copy()
                                            m_copy.apply_transform(total_transform)
                                            pm = pv.wrap(m_copy)
                                            actor_name = f"layout_{id_short}_{getattr(geom, 'name', 'link')}_{id(m)}"
                                            
                                            # Get color matching Unity/URDF
                                            visual_obj = geom_to_visual.get(geom, None)
                                            col = [0.7, 0.7, 0.7]
                                            if visual_obj and hasattr(visual_obj, 'material') and visual_obj.material and visual_obj.material.color is not None:
                                                col = np.array(visual_obj.material.color[:3]).astype(float)
                                                if col.max() > 1.01:
                                                    col /= 255.0
                                            elif "abb" in id_short.lower():
                                                col = [246.0/255.0, 120.0/255.0, 40.0/255.0]
                                                
                                            ac = self.plotter.add_mesh(pm, name=actor_name, color=col, smooth_shading=True, specular=0.5, ambient=0.2, show_scalar_bar=False)
                                            self.layout_actor_names.append(actor_name)
                                        
                                    loaded_count += 1
                                except Exception as e_robot:
                                    print(f"Error loading robot in layout preview: {e_robot}")
                                    traceback.print_exc()

                    if vis_data:
                        # Extract behaviors for visibility filtering (like presence sensors, conveyors)
                        beh_data = next((v for k,v in submodels_data.items() if "BehaviorMapping" in k or "Behavior" in k), None)
                        hidden_nodes = set()
                        if beh_data:
                            act_types = ["Conveyor", "RotateContinuous", "TranslateContinuous", "Piston", "Linear"]
                            sens_types = ["TriggerZone", "Presence", "Proximity"]
                            for el in beh_data.get("submodelElements", []):
                                if el["modelType"] == "SubmodelElementCollection":
                                    props = {p["idShort"]: p.get("value") for p in el.get("value", []) if p["modelType"] == "Property"}
                                    params = {}
                                    for p in el.get("value", []):
                                        if p["idShort"] == "Parameters": params = {pp["idShort"]: pp.get("value") for pp in p.get("value", []) if pp["modelType"] == "Property"}
                                    
                                    b_type = props.get("Type", ""); node = props.get("Component", "")
                                    if b_type in act_types:
                                        visible = str(params.get("Visible", "True")).lower() == "true"
                                        if not visible and node: hidden_nodes.add(node)
                                    elif b_type in sens_types:
                                        visible = str(params.get("Visible", "False")).lower() == "true"
                                        if not visible and node: hidden_nodes.add(node)

                        # Parse transform
                        pos_val = [0.0]*3; rot_val = [0.0]*3; scale_val = [1.0]*3
                        mesh_href = None
                        coll_id_short = None
                        for el in vis_data.get("submodelElements", []):
                            if el["modelType"] == "SubmodelElementCollection": 
                                for sel in el.get("value", []):
                                    if sel["idShort"] == "Transform":
                                        for t in sel.get("value", []):
                                            v = float(t.get("value", 0))
                                            if t["idShort"] == "PosX": pos_val[0] = v
                                            elif t["idShort"] == "PosY": pos_val[1] = v
                                            elif t["idShort"] == "PosZ": pos_val[2] = v
                                            elif t["idShort"] == "RotX": rot_val[0] = v
                                            elif t["idShort"] == "RotY": rot_val[1] = v
                                            elif t["idShort"] == "RotZ": rot_val[2] = v
                                            elif t["idShort"] == "ScaleX": scale_val[0] = v
                                            elif t["idShort"] == "ScaleY": scale_val[1] = v
                                            elif t["idShort"] == "ScaleZ": scale_val[2] = v
                                    elif sel["idShort"] == "Mesh" and sel["modelType"] == "File":
                                        mesh_href = sel.get("value")
                                        coll_id_short = el["idShort"]

                        if mesh_href and coll_id_short:
                            # Fetch mesh attachment
                            vis_id_node = vis_data.get("id")
                            encoded_vis_id = base64.urlsafe_b64encode(vis_id_node.encode()).decode().rstrip("=")
                            dl_url = f"http://localhost:8081/submodels/{encoded_vis_id}/submodel-elements/{coll_id_short}.Mesh/attachment"
                            m_res = requests.get(dl_url, timeout=3)
                            if m_res.status_code == 200:
                                import tempfile
                                tp = os.path.join(tempfile.gettempdir(), f"{id_short}_layout_bg.glb")
                                with open(tp, "wb") as f: f.write(m_res.content)
                                
                                # Load geometry
                                scene = trimesh.load(tp, process=False)
                                
                                # Build transformation matrix M = T * R * S
                                # Map GLB Y-up coordinates to Unity left-handed coordinates (reflected Z-flip)
                                pos_pv = [pos_val[0], pos_val[1], pos_val[2]]
                                rz, rx, ry = rot_val[2], -rot_val[0], -rot_val[1]
                                scale_pv = [scale_val[0], scale_val[1], scale_val[2]]
                                
                                T = tf.translation_matrix(pos_pv)
                                R = tf.euler_matrix(np.radians(rz), np.radians(rx), np.radians(ry), 'szxy')
                                S = np.diag([scale_pv[0], scale_pv[1], scale_pv[2], 1.0])
                                M = tf.concatenate_matrices(T, R, S)
                                
                                if isinstance(scene, trimesh.Scene):
                                    geo_mats = {}
                                    for gn, mesh in scene.geometry.items():
                                        tx = None; cl = None; mt = 0.0; rg = 0.5
                                        if hasattr(mesh, 'visual') and hasattr(mesh.visual, 'material'):
                                            mat = mesh.visual.material; img = getattr(mat, 'image', getattr(mat, 'baseColorTexture', None))
                                            if img:
                                                try: tx = pv.Texture(np.array(img))
                                                except: pass
                                            if hasattr(mat, 'baseColorFactor') and mat.baseColorFactor is not None:
                                                cl = mat.baseColorFactor[:3]
                                            mt = getattr(mat, 'metallicFactor', 0.0)
                                            rg = getattr(mat, 'roughnessFactor', 0.5)
                                        geo_mats[gn] = (tx, cl, mt, rg)
                                    
                                    root_nodes = [node for node in scene.graph.nodes if scene.graph.transforms.parents.get(node) is None]
                                    
                                    def is_hidden(node):
                                        curr = node
                                        while curr is not None:
                                            if curr in hidden_nodes: return True
                                            curr = scene.graph.transforms.parents.get(curr)
                                        return False

                                    def add_preview_node(node):
                                        if node in scene.graph.nodes_geometry:
                                            if not is_hidden(node):
                                                node_tf, gn = scene.graph[node]
                                                mi = scene.geometry[gn].copy()
                                                combined_tf = M @ node_tf
                                                mi.apply_transform(combined_tf)
                                                pm = pv.wrap(mi)
                                                tx, cl, mt, rg = geo_mats.get(gn, (None, None, 0.0, 0.5))
                                                
                                                # Use a unique name with layout_ prefix
                                                actor_name = f"layout_{id_short}_{node}"
                                                
                                                # Make layout preview meshes slightly semi-transparent (e.g. opacity=0.7) to differentiate them
                                                ac = self.plotter.add_mesh(pm, name=actor_name, texture=tx, color=cl if not tx else None, 
                                                                           smooth_shading=True, pbr=True, metallic=mt, roughness=rg,
                                                                           opacity=0.7, show_scalar_bar=False)
                                                self.layout_actor_names.append(actor_name)
                                        for c in scene.graph.transforms.children.get(node, []):
                                            add_preview_node(c)
                                            
                                    for r in root_nodes:
                                        add_preview_node(r)
                                elif isinstance(scene, trimesh.Trimesh):
                                    pm = pv.wrap(scene)
                                    pm.transform(M)
                                    actor_name = f"layout_{id_short}_main"
                                    ac = self.plotter.add_mesh(pm, name=actor_name, pbr=True, opacity=0.7, show_scalar_bar=False)
                                    self.layout_actor_names.append(actor_name)
                                
                                loaded_count += 1
            
            # Re-render
            self.plotter.render()
            self.update_system_transform()
        except Exception as ex:
            QMessageBox.warning(self, "Warning", f"Error rendering some layout items: {ex}")
            traceback.print_exc()
        finally:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def next_step(self):
        idx = self.stack.currentIndex()
        if self.model_type == "URDF" and idx == 1:
            self.stack.setCurrentIndex(4) # Jump directly to Preview page (former 5, now 4)
            self.update_nav()
        elif idx < self.stack.count() - 1:
            self.stack.setCurrentIndex(idx + 1)
            self.update_nav()
        else:
            self.deploy_to_aas()

    def prev_step(self):
        idx = self.stack.currentIndex()
        if self.model_type == "URDF" and idx == 4:
            self.stack.setCurrentIndex(1)
            self.update_nav()
        else:
            self.stack.setCurrentIndex(idx - 1)
            self.update_nav()

    def update_nav(self):
        idx = self.stack.currentIndex(); self.btn_prev.setEnabled(idx > 0)
        if self.model_type == "URDF":
            urdf_steps = {
                0: "Step 1 of 3: Model Selection",
                1: "Step 2 of 3: Basic Info & Transform",
                4: "Step 3 of 3: Review & Deploy"
            }
            self.step_label.setText(urdf_steps.get(idx, f"Step {idx+1} of 3"))
        else:
            titles = ["Model Selection", "Basic Info & Transform", "Configure Sensors", "Configure Actuators", "Review & Deploy"]
            self.step_label.setText(f"Step {idx+1} of 5: {titles[idx]}")
            
        if idx == 3:
            active_found = False
            for w in self.actuator_widgets:
                if w.property("active") == "true":
                    w.update_visuals()
                    active_found = True
                else:
                    w.hide_visuals()
            if not active_found and self.actuator_widgets:
                self.actuator_widgets[0].activate()
        elif idx == 4:
            for w in self.actuator_widgets:
                w.update_visuals(force=True)
        else:
            for w in self.actuator_widgets:
                w.hide_visuals()
            
        self.btn_next.setText("Deploy to AAS" if idx == self.stack.count() - 1 else "Next →"); self.update_json_preview()
            
    def update_json_preview(self):
        if self.model_type == "URDF":
            self.config_data = {
                "robot_name": self.robot.name if (self.robot and self.robot.name) else "Unnamed Robot",
                "aas_id": self.edit_aas_id.text(),
                "description": self.edit_desc.text(),
                "model_type": "URDF",
                "position": [s.value() for s in self.sys_pos],
                "rotation": [s.value() for s in self.sys_rot],
                "scale": [s.value() for s in self.sys_scale],
                "joints": [
                    {
                        "name": j.name,
                        "type": j.joint_type,
                        "parent": j.parent,
                        "child": j.child,
                        "limit": {
                            "lower": j.limit.lower if j.limit else None,
                            "upper": j.limit.upper if j.limit else None,
                            "velocity": j.limit.velocity if j.limit else None,
                            "effort": j.limit.effort if j.limit else None
                        } if j.limit else None
                    } for j in self.robot.joints
                ] if self.robot else []
            }
            self.json_preview.setText(json.dumps(self.config_data, indent=2))
            return

        self.config_data = {
            "kit_name": self.edit_kit_name.text(),
            "aas_id": self.edit_aas_id.text(),
            "description": self.edit_desc.text(),
            "comm_protocol": self.combo_comm_protocol.currentText(),
            "comm_ip": self.edit_comm_ip.text().strip(),
            "comm_port": self.spin_comm_port.value(),
            "control_logic": self.logic_rules
        }
        
        comp_name = "Product_ConveyorSystem" if self.check_is_dynamic.isChecked() else "ConveyorSystem"
        comp = {
            "name": comp_name,
            "mesh": os.path.basename(self.glb_path) if self.glb_path else "",
            "parent": None,
            "position": [s.value() for s in self.sys_pos],
            "rotation": [s.value() for s in self.sys_rot],
            "scale": [s.value() for s in self.sys_scale],
            "dynamic": self.check_is_dynamic.isChecked(),
            "actuators": [w.get_data() for w in self.actuator_widgets],
            "sensors": [w.get_data() for w in self.sensor_widgets]
        }
        self.config_data["components"] = [comp]
        self.json_preview.setText(json.dumps(self.config_data, indent=2))

    def deploy_to_aas(self, silent=False):
        self.update_json_preview()
        has_mesh = bool(self.glb_path and os.path.exists(self.glb_path))
        if self.model_type == "URDF":
            if not has_mesh or not getattr(self, "current_urdf_tree", None):
                if not silent:
                    QMessageBox.warning(self, "Error", "Select a valid URDF robot model.")
                else:
                    raise Exception("Select a valid URDF robot model.")
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.btn_next.setEnabled(False); self.btn_next.setText("Deploying...")
        try:
            kn = self.edit_kit_name.text().strip() or "AAS_System"
            aid = self.edit_aas_id.text().strip() or kn
            kfn = sanitize_aas_id(kn)
            if self.model_type == "URDF":
                px, py, pz = [str(s.value()) for s in self.sys_pos]
                rx, ry, rz = [str(s.value()) for s in self.sys_rot]
                sx, sy, sz = [str(s.value()) for s in self.sys_scale]
                units = "rad"
                import copy
                tree_copy = copy.deepcopy(self.current_urdf_tree)
                manager = AssetManager(self.current_robot_root)
                print(f"[DEPLOY] Starting asset collection from resolved URDF tree...")
                for elem in tree_copy.iter():
                    for attr, val in elem.attrib.items():
                        if val and any(ext in val.lower() for ext in manager.exts):
                            rel = manager.collect(val, self.resolver)
                            if rel: elem.set(attr, rel)
                urdf_filename = os.path.basename(self.current_urdf_path)
                if urdf_filename.lower().endswith('.xacro'):
                    urdf_filename = re.sub(r'\.xacro$', '', urdf_filename, flags=re.IGNORECASE)
                if not urdf_filename.lower().endswith('.urdf'):
                    urdf_filename += '.urdf'
                urdf_rel_path = urdf_filename
                urdf_bytes = io.BytesIO()
                ET.ElementTree(tree_copy).write(urdf_bytes, encoding='utf-8', xml_declaration=True)
                shell = model.AssetAdministrationShell(asset_information=model.AssetInformation(asset_kind=model.AssetKind.INSTANCE, global_asset_id=f"http://acplt.org/Assets/{kfn}"), id_=f"https://acplt.org/AAS_{kfn}", id_short=aid)
                sm_geo = model.Submodel(id_=f"https://acplt.org/Submodels/Geo_{kfn}", id_short="GeometryData")
                sm_pos = model.Submodel(id_=f"https://acplt.org/Submodels/Pos_{kfn}", id_short="PositionData")
                sm_tel = model.Submodel(id_=f"https://acplt.org/Submodels/Tel_{kfn}", id_short="TelemetryData")
                for ax, v in [('X', px), ('Y', py), ('Z', pz)]:
                    sm_pos.submodel_element.add(model.Property(id_short=f"Pos_{ax}", value_type=model.datatypes.Double, value=float(v.replace(',', '.'))))
                for ax, v in [('X', rx), ('Y', ry), ('Z', rz)]:
                    sm_pos.submodel_element.add(model.Property(id_short=f"Rot_{ax}", value_type=model.datatypes.Double, value=float(v.replace(',', '.'))))
                for ax, v in [('X', sx), ('Y', sy), ('Z', sz)]:
                    sm_pos.submodel_element.add(model.Property(id_short=f"Scale_{ax}", value_type=model.datatypes.Double, value=float(v.replace(',', '.'))))
                sm_tel.submodel_element.add(model.Property(id_short="JointUnits", value_type=model.datatypes.String, value=units))
                joints_coll = model.SubmodelElementCollection(id_short="LiveJoints")
                for j in self.robot.actuated_joints:
                    joints_coll.value.add(model.Property(id_short=sanitize_aas_id(j.name), value_type=model.datatypes.Double, value=0.0))
                sm_tel.submodel_element.add(joints_coll)
                file_repo = RobustFileRepo()
                aas_urdf_path = posixpath.normpath(posixpath.join("/aasx/robot", urdf_rel_path))
                file_repo.add_file(aas_urdf_path, urdf_bytes.getvalue())
                urdf_id = sanitize_aas_id("PATH_" + urdf_rel_path.replace("/", "_SL_").replace(".", "_DOT_").replace("-", "_DASH_"))
                sm_geo.submodel_element.add(model.File(id_short=urdf_id[:100], value=aas_urdf_path, content_type="application/xml"))
                for l_path, r_path in manager.files_to_upload.items():
                    aas_path = posixpath.normpath(posixpath.join("/aasx/robot", r_path))
                    if aas_path == aas_urdf_path: continue
                    try:
                        with open(l_path, 'rb') as f: file_repo.add_file(aas_path, f.read())
                        pid = sanitize_aas_id("PATH_" + r_path.replace("/", "_SL_").replace(".", "_DOT_").replace("-", "_DASH_"))
                        sm_geo.submodel_element.add(model.File(id_short=pid[:100], value=aas_path, content_type=mimetypes.guess_type(l_path)[0] or "application/octet-stream"))
                    except Exception as e: pass
                obj_store = model.DictObjectStore()
                obj_store.add(shell); obj_store.add(sm_geo); obj_store.add(sm_pos); obj_store.add(sm_tel)
                shell.submodel.add(model.ModelReference.from_referable(sm_geo))
                shell.submodel.add(model.ModelReference.from_referable(sm_pos))
                shell.submodel.add(model.ModelReference.from_referable(sm_tel))
                os.makedirs("aasx_packages", exist_ok=True); out = os.path.join("aasx_packages", f"{kfn}_Twin.aasx")
                with aasx.AASXWriter(out) as w: w.write_aas(shell.id, obj_store, file_repo, aid)
                # Check for existence beforehand to detect conflict reliably
                existing_exists = False
                try:
                    enc_shell = base64.urlsafe_b64encode(shell.id.encode()).decode().rstrip("=")
                    if requests.get(f"http://localhost:8081/shells/{enc_shell}", timeout=3).status_code == 200:
                        existing_exists = True
                    else:
                        for sm in [sm_geo, sm_pos, sm_tel]:
                            enc_sm = base64.urlsafe_b64encode(sm.id.encode()).decode().rstrip("=")
                            if requests.get(f"http://localhost:8081/submodels/{enc_sm}", timeout=3).status_code == 200:
                                existing_exists = True
                                break
                except Exception:
                    pass

                is_conflict = existing_exists
                res = None
                if not is_conflict:
                    res = requests.post("http://localhost:8081/upload", files={'file': (os.path.basename(out), open(out, 'rb'), 'application/octet-stream')}, timeout=10)
                    if res.status_code == 409 or (res.text and ("Duplicate element id" in res.text or "CollidingIdentifierException" in res.text)):
                        is_conflict = True
                
                if is_conflict:
                    while QApplication.overrideCursor() is not None:
                        QApplication.restoreOverrideCursor()
                    if not silent:
                        reply = QMessageBox.question(
                            self, "AAS Already Exists",
                            f"An AAS with ID '{shell.id}' already exists. Would you like to overwrite it?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                        )
                        if reply != QMessageBox.StandardButton.Yes:
                            return
                    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                    self.delete_aas_by_id(shell.id, [sm_geo.id, sm_pos.id, sm_tel.id])
                    time.sleep(0.5)
                    with open(out, 'rb') as f:
                        res = requests.post("http://localhost:8081/upload", files={'file': (os.path.basename(out), f, 'application/octet-stream')}, timeout=10)
                    if res.status_code in [200, 201, 204]:
                        requests.post("http://localhost:8082/shell-descriptors", json={"id": shell.id, "idShort": shell.id_short, "assetKind": "Instance", "globalAssetId": shell.asset_information.global_asset_id, "submodelDescriptors": [{"idShort": s.id_short, "id": s.id, "endpoints": [{"interface": "SUBMODEL-3.0", "protocolInformation": {"href": f"http://localhost:8081/submodels/{s.id}", "endpointProtocol": "HTTP"}}]} for s in [sm_geo, sm_pos, sm_tel]], "endpoints": [{"interface": "AAS-3.0", "protocolInformation": {"href": f"http://localhost:8081/shells/{shell.id}", "endpointProtocol": "HTTP"}}]}, timeout=10)
                        while QApplication.overrideCursor() is not None:
                            QApplication.restoreOverrideCursor()
                        if not silent:
                            QMessageBox.information(self, "Success", f"AAS Robot deployed (overwritten) successfully!")
                            self.reset_wizard()
                            self.stack.setCurrentIndex(0)
                            self.update_nav()
                    else:
                        raise Exception(f"Upload failed after overwrite: {res.text}")
                elif res.status_code in [200, 201, 204]:
                    requests.post("http://localhost:8082/shell-descriptors", json={"id": shell.id, "idShort": shell.id_short, "assetKind": "Instance", "globalAssetId": shell.asset_information.global_asset_id, "submodelDescriptors": [{"idShort": s.id_short, "id": s.id, "endpoints": [{"interface": "SUBMODEL-3.0", "protocolInformation": {"href": f"http://localhost:8081/submodels/{s.id}", "endpointProtocol": "HTTP"}}]} for s in [sm_geo, sm_pos, sm_tel]], "endpoints": [{"interface": "AAS-3.0", "protocolInformation": {"href": f"http://localhost:8081/shells/{shell.id}", "endpointProtocol": "HTTP"}}]}, timeout=10)
                    while QApplication.overrideCursor() is not None:
                        QApplication.restoreOverrideCursor()
                    if not silent:
                        QMessageBox.information(self, "Success", f"AAS Robot deployed successfully!")
                        self.reset_wizard()
                        self.stack.setCurrentIndex(0)
                        self.update_nav()
                else: raise Exception(f"Upload failed: {res.text}")
                return

            # Strict Validation of Numeric Fields
            try:
                for c in self.config_data.get("components", []):
                    for act in c.get("actuators", []):
                        if "axis" in act:
                            for val in act["axis"]:
                                float(str(val).replace(',', '.'))
                        if "speed" in act:
                            float(str(act["speed"]).replace(',', '.'))
                    for sns in c.get("sensors", []):
                        if "move_position" in sns:
                            for val in sns["move_position"]:
                                float(str(val).replace(',', '.'))
                        if "move_rotation" in sns:
                            for val in sns["move_rotation"]:
                                float(str(val).replace(',', '.'))
                        if "move_duration" in sns:
                            float(str(sns["move_duration"]).replace(',', '.'))
            except ValueError as val_err:
                raise Exception(f"Invalid numeric format detected. Please check your coordinate/duration values: {val_err}")

            shell = model.AssetAdministrationShell(asset_information=model.AssetInformation(asset_kind=model.AssetKind.INSTANCE, global_asset_id=f"http://acplt.org/Assets/{kfn}"), id_=f"https://acplt.org/AAS_{kfn}", id_short=aid)
            submodels_to_deploy = []

            # 1. VisualStructure (ONLY if 3D mesh model exists)
            if has_mesh:
                vs = model.Submodel(id_=f"https://acplt.org/Submodels/Visual_{kfn}", id_short="VisualStructure")
                for c in self.config_data.get("components", []):
                    cc = model.SubmodelElementCollection(id_short=sanitize_aas_id(c["name"])); mf = c.get("mesh", "")
                    if mf: cc.value.add(model.File(id_short="Mesh", content_type="application/octet-stream", value=mf))
                    cc.value.add(model.Property(id_short="Parent", value_type=model.datatypes.String, value=c.get("parent", "")))
                    is_prod = c.get("dynamic", False) or (c["name"] == "Product_ConveyorSystem") or self.check_is_dynamic.isChecked()
                    cc.value.add(model.Property(id_short="DynamicObject", value_type=model.datatypes.Boolean, value=is_prod))
                    tc = model.SubmodelElementCollection(id_short="Transform"); ps = c.get("position", [0,0,0]); rt = c.get("rotation", [0,0,0]); sc = c.get("scale", [1,1,1])
                    for i, a in enumerate(["X", "Y", "Z"]): tc.value.add(model.Property(id_short=f"Pos{a}", value_type=model.datatypes.Double, value=float(str(ps[i]).replace(',', '.'))))
                    for i, a in enumerate(["X", "Y", "Z"]): tc.value.add(model.Property(id_short=f"Rot{a}", value_type=model.datatypes.Double, value=float(str(rt[i]).replace(',', '.'))))
                    for i, a in enumerate(["X", "Y", "Z"]): tc.value.add(model.Property(id_short=f"Scale{a}", value_type=model.datatypes.Double, value=float(str(sc[i]).replace(',', '.'))))
                    cc.value.add(tc); vs.submodel_element.add(cc)
                submodels_to_deploy.append(vs)

            # 2. VirtualPins & BehaviorMapping
            has_behavior = any(c.get("actuators") or c.get("sensors") for c in self.config_data.get("components", [])) or bool(self.actuator_widgets) or bool(self.sensor_widgets)
            if has_mesh or has_behavior:
                vp = model.Submodel(id_=f"https://acplt.org/Submodels/Pins_{kfn}", id_short="VirtualPins")
                bm = model.Submodel(id_=f"https://acplt.org/Submodels/Behavior_{kfn}", id_short="BehaviorMapping")
                ic = model.SubmodelElementCollection(id_short="Inputs"); oc = model.SubmodelElementCollection(id_short="Outputs")
                vp.submodel_element.add(ic); vp.submodel_element.add(oc)
                for c in self.config_data.get("components", []):
                    for act in c.get("actuators", []):
                        pid = act["pin_id"]; tco = act.get("node", c["name"]); ep = [p.id_short for p in oc.value]
                        safe_pid = sanitize_aas_id(pid)
                        if safe_pid not in ep: oc.value.add(model.Property(id_short=safe_pid, value_type=model.datatypes.Boolean, value=False))
                        bc = model.SubmodelElementCollection(id_short=sanitize_aas_id(f"{pid}_{tco}")); bc.value.add(model.Property(id_short="PinID", value_type=model.datatypes.String, value=pid))
                        bc.value.add(model.Property(id_short="Component", value_type=model.datatypes.String, value=tco)); bc.value.add(model.Property(id_short="Type", value_type=model.datatypes.String, value=act.get("behavior")))
                        pc = model.SubmodelElementCollection(id_short="Parameters")
                        if "axis" in act:
                            for i, a in enumerate(["X", "Y", "Z"]): pc.value.add(model.Property(id_short=f"Axis{a}", value_type=model.datatypes.Double, value=float(str(act["axis"][i]).replace(',', '.'))))
                        if "speed" in act: pc.value.add(model.Property(id_short="Speed", value_type=model.datatypes.Double, value=float(str(act["speed"]).replace(',', '.'))))
                        if "visible" in act: pc.value.add(model.Property(id_short="Visible", value_type=model.datatypes.Boolean, value=bool(act["visible"])))
                        if "stop_sensor" in act and act["stop_sensor"]: pc.value.add(model.Property(id_short="StopSensorComponent", value_type=model.datatypes.String, value=act["stop_sensor"]))
                        pc.value.add(model.Property(id_short="RealActuatorEnabled", value_type=model.datatypes.Boolean, value=bool(act.get("real_actuator_enabled", False))))
                        pc.value.add(model.Property(id_short="RealPortID", value_type=model.datatypes.String, value=str(act.get("real_port_id", ""))))
                        bc.value.add(pc); bm.submodel_element.add(bc)
                    for sns in c.get("sensors", []):
                        pid = sns["pin_id"]; tco = sns.get("node", c["name"]); ep = [p.id_short for p in ic.value]; safe_pid = sanitize_aas_id(pid)
                        if safe_pid not in ep: ic.value.add(model.Property(id_short=safe_pid, value_type=model.datatypes.Boolean, value=False))
                        bc = model.SubmodelElementCollection(id_short=sanitize_aas_id(f"{pid}_{tco}")); bc.value.add(model.Property(id_short="PinID", value_type=model.datatypes.String, value=pid)); bc.value.add(model.Property(id_short="Component", value_type=model.datatypes.String, value=tco)); bc.value.add(model.Property(id_short="Type", value_type=model.datatypes.String, value=sns.get("behavior")))
                        pc = model.SubmodelElementCollection(id_short="Parameters")
                        if "target_filter" in sns: pc.value.add(model.Property(id_short="TargetFilter", value_type=model.datatypes.String, value=sns["target_filter"]))
                        if "visible" in sns: pc.value.add(model.Property(id_short="Visible", value_type=model.datatypes.Boolean, value=bool(sns["visible"])))
                        if "active_low" in sns: pc.value.add(model.Property(id_short="ActiveLow", value_type=model.datatypes.Boolean, value=bool(sns["active_low"])))
                        if sns.get("snap_product"):
                            pc.value.add(model.Property(id_short="SnapTarget", value_type=model.datatypes.Boolean, value=True))
                        pc.value.add(model.Property(id_short="RealSensorEnabled", value_type=model.datatypes.Boolean, value=bool(sns.get("real_sensor_enabled", False))))
                        pc.value.add(model.Property(id_short="RealPortID", value_type=model.datatypes.String, value=str(sns.get("real_port_id", ""))))
                        if "move_target" in sns:
                            pc.value.add(model.Property(id_short="MoveTarget", value_type=model.datatypes.String, value=sns["move_target"]))
                        if "move_on_state" in sns:
                            pc.value.add(model.Property(id_short="MoveOnState", value_type=model.datatypes.Boolean, value=bool(sns["move_on_state"])))
                        if "move_mode" in sns:
                            pc.value.add(model.Property(id_short="MoveMode", value_type=model.datatypes.String, value=sns["move_mode"]))
                        if "move_position" in sns:
                            mp = sns["move_position"]
                            for i, a in enumerate(["X", "Y", "Z"]): pc.value.add(model.Property(id_short=f"MovePos{a}", value_type=model.datatypes.Double, value=float(str(mp[i]).replace(',', '.'))))
                        if "move_rotation" in sns:
                            mr = sns["move_rotation"]
                            for i, a in enumerate(["X", "Y", "Z"]): pc.value.add(model.Property(id_short=f"MoveRot{a}", value_type=model.datatypes.Double, value=float(str(mr[i]).replace(',', '.'))))
                        if "move_duration" in sns:
                            pc.value.add(model.Property(id_short="MoveDuration", value_type=model.datatypes.Double, value=float(str(sns["move_duration"]).replace(',', '.'))))
                        bc.value.add(pc); bm.submodel_element.add(bc)
                submodels_to_deploy.extend([vp, bm])

            # 3. AssetInterfacesDescription
            comm_proto = str(self.combo_comm_protocol.currentText()) if hasattr(self, "combo_comm_protocol") else "Sockets"
            if comm_proto and comm_proto != "None":
                sm_aid = model.Submodel(id_=f"https://acplt.org/Submodels/AssetInterfacesDescription_{kfn}", id_short="AssetInterfacesDescription")
                sm_aid.submodel_element.add(model.Property(id_short="Protocol", value_type=model.datatypes.String, value=comm_proto))
                sm_aid.submodel_element.add(model.Property(id_short="EndpointIP", value_type=model.datatypes.String, value=str(self.edit_comm_ip.text().strip())))
                try:
                    port_val = int(self.spin_comm_port.value())
                except Exception:
                    port_val = 5000
                sm_aid.submodel_element.add(model.Property(id_short="EndpointPort", value_type=model.datatypes.Integer, value=port_val))
                submodels_to_deploy.append(sm_aid)

            # 4. ControlLogic
            has_logic = hasattr(self, 'logic_rules') and self.logic_rules and (self.logic_rules.get("start") or self.logic_rules.get("sequences"))
            if has_logic or not has_mesh:
                rules_val = self.logic_rules if (hasattr(self, 'logic_rules') and self.logic_rules) else {"start": [], "sequences": []}
                sm_logic = model.Submodel(id_=f"https://acplt.org/Submodels/ControlLogic_{kfn}", id_short="ControlLogic")
                sm_logic.submodel_element.add(model.Property(id_short="Rules", value_type=model.datatypes.String, value=json.dumps(rules_val)))
                submodels_to_deploy.append(sm_logic)

            for sm in submodels_to_deploy:
                shell.submodel.add(model.ModelReference.from_referable(sm))

            obj_store = model.DictObjectStore()
            obj_store.add(shell)
            for sm in submodels_to_deploy:
                obj_store.add(sm)

            fr = RobustFileRepo()
            if has_mesh:
                import tempfile
                bd = os.path.dirname(self.glb_path)
                ae = ['.obj', '.mtl', '.stl', '.png', '.jpg', '.jpeg', '.dae', '.fbx', '.glb', '.gltf', '.bin']
                is_temp_dir = os.path.abspath(bd) == os.path.abspath(tempfile.gettempdir())
                
                if is_temp_dir:
                    fn = os.path.basename(self.glb_path)
                    with open(self.glb_path, 'rb') as tf:
                        co = tf.read()
                    ap = posixpath.normpath(posixpath.join("/aasx/kit", fn))
                    fr.add_file(ap, co)
                else:
                    for r, d, fs in os.walk(bd):
                        for f in fs:
                            if any(f.lower().endswith(ext) for ext in ae):
                                lp = os.path.join(r, f); rp = os.path.relpath(lp, bd).replace("\\", "/")
                                with open(lp, 'rb') as tf:
                                    co = tf.read()
                                ap = posixpath.normpath(posixpath.join("/aasx/kit", rp)); fr.add_file(ap, co)

                for sm in submodels_to_deploy:
                    for el in sm.submodel_element:
                        if isinstance(el, model.File) and el.value and not el.value.startswith("/aasx/"): el.value = posixpath.normpath(posixpath.join("/aasx/kit", el.value))
                        elif isinstance(el, model.SubmodelElementCollection):
                            for se in el.value:
                                if isinstance(se, model.File) and se.value and not se.value.startswith("/aasx/"): se.value = posixpath.normpath(posixpath.join("/aasx/kit", se.value))

            os.makedirs("aasx_packages", exist_ok=True); out = os.path.join("aasx_packages", f"{kfn}_Kit.aasx")
            with aasx.AASXWriter(out) as w: w.write_aas(shell.id, obj_store, fr, aid)
            
            # Check for existence beforehand to detect conflict reliably
            existing_exists = False
            try:
                enc_shell = base64.urlsafe_b64encode(shell.id.encode()).decode().rstrip("=")
                if requests.get(f"http://localhost:8081/shells/{enc_shell}", timeout=3).status_code == 200:
                    existing_exists = True
                else:
                    for sm in submodels_to_deploy:
                        enc_sm = base64.urlsafe_b64encode(sm.id.encode()).decode().rstrip("=")
                        if requests.get(f"http://localhost:8081/submodels/{enc_sm}", timeout=3).status_code == 200:
                            existing_exists = True
                            break
            except Exception:
                pass

            is_conflict = existing_exists
            res = None
            if not is_conflict:
                with open(out, 'rb') as f: 
                    res = requests.post("http://localhost:8081/upload", files={'file': (os.path.basename(out), f, 'application/octet-stream')}, timeout=10)
                if res.status_code == 409 or (res.text and ("Duplicate element id" in res.text or "CollidingIdentifierException" in res.text)):
                    is_conflict = True
 
            if is_conflict:
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                if not silent:
                    reply = QMessageBox.question(
                        self, "AAS Already Exists",
                        f"An AAS with ID '{shell.id}' already exists. Would you like to overwrite it?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                # Delete the existing AAS and submodels
                self.delete_aas_by_id(shell.id, [s.id for s in submodels_to_deploy])
                time.sleep(0.5)  # Wait for BaSyx database to complete deletion & release file locks
                # Retry upload
                with open(out, 'rb') as f:
                    res = requests.post("http://localhost:8081/upload", files={'file': (os.path.basename(out), f, 'application/octet-stream')}, timeout=10)
                
                if res.status_code not in [200, 201, 204]:
                    raise Exception(f"BaSyx Upload failed (HTTP {res.status_code}): {res.text}")
                    
                reg_res = requests.post("http://localhost:8082/shell-descriptors", json={"id": shell.id, "idShort": shell.id_short, "assetKind": "Instance", "globalAssetId": shell.asset_information.global_asset_id, "submodelDescriptors": [{"idShort": s.id_short, "id": s.id, "endpoints": [{"interface": "SUBMODEL-3.0", "protocolInformation": {"href": f"http://localhost:8081/submodels/{s.id}", "endpointProtocol": "HTTP"}}]} for s in submodels_to_deploy], "endpoints": [{"interface": "AAS-3.0", "protocolInformation": {"href": f"http://localhost:8081/shells/{shell.id}", "endpointProtocol": "HTTP"}}]}, timeout=10)
                if reg_res.status_code not in [200, 201, 204, 409]:
                    raise Exception(f"BaSyx Registry failed (HTTP {reg_res.status_code}): {reg_res.text}")
                    
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                if not silent:
                    QMessageBox.information(self, "Success", f"AAS deployed (overwritten) successfully!")
                    self.reset_wizard()
                    self.stack.setCurrentIndex(0)
                    self.update_nav()
            elif res.status_code in [200, 201, 204]:
                reg_res = requests.post("http://localhost:8082/shell-descriptors", json={"id": shell.id, "idShort": shell.id_short, "assetKind": "Instance", "globalAssetId": shell.asset_information.global_asset_id, "submodelDescriptors": [{"idShort": s.id_short, "id": s.id, "endpoints": [{"interface": "SUBMODEL-3.0", "protocolInformation": {"href": f"http://localhost:8081/submodels/{s.id}", "endpointProtocol": "HTTP"}}]} for s in submodels_to_deploy], "endpoints": [{"interface": "AAS-3.0", "protocolInformation": {"href": f"http://localhost:8081/shells/{shell.id}", "endpointProtocol": "HTTP"}}]}, timeout=10)
                if reg_res.status_code not in [200, 201, 204, 409]:
                    raise Exception(f"BaSyx Registry failed (HTTP {reg_res.status_code}): {reg_res.text}")
                    
                while QApplication.overrideCursor() is not None:
                    QApplication.restoreOverrideCursor()
                if not silent:
                    QMessageBox.information(self, "Success", f"AAS deployed successfully!")
                    self.reset_wizard()
                    self.stack.setCurrentIndex(0)
                    self.update_nav()
            else:
                raise Exception(f"BaSyx Upload failed (HTTP {res.status_code}): {res.text}")
            return
        except Exception as e:
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            traceback.print_exc()
            if not silent:
                QMessageBox.critical(self, "Deployment Error", f"Failed to deploy AAS to BaSyx server.\n\nDetails:\n{str(e)}")
            else:
                raise e
        finally:
            self.btn_next.setEnabled(True); self.btn_next.setText("Deploy to AAS")
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

class TriggerRow(QWidget):
    def __init__(self, parent=None, catalog=None, initial_sensor="", initial_value=True, on_delete=None, on_changed=None, initial_kit=None):
        super().__init__(parent)
        self.catalog = catalog or []
        self.on_delete = on_delete
        self.on_changed = on_changed
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        
        self.combo_sensor = QComboBox()
        self.combo_sensor.addItems(self.catalog)
        
        target_text = f"{initial_kit} - {initial_sensor}" if initial_kit and initial_sensor else initial_sensor
        if target_text in self.catalog:
            self.combo_sensor.setCurrentText(target_text)
        elif initial_sensor in self.catalog:
            self.combo_sensor.setCurrentText(initial_sensor)
        else:
            for item in self.catalog:
                if item.endswith(f" - {initial_sensor}"):
                    self.combo_sensor.setCurrentText(item)
                    break
                    
        self.check_val = QCheckBox("Is Active")
        self.check_val.setChecked(initial_value)
        
        self.btn_del = QPushButton("❌")
        self.btn_del.setFixedWidth(30)
        self.btn_del.setStyleSheet("background-color: #a93226;")
        
        layout.addWidget(self.combo_sensor, stretch=2)
        layout.addWidget(self.check_val)
        layout.addWidget(self.btn_del)
        
        self.combo_sensor.currentTextChanged.connect(self._notify_change)
        self.check_val.toggled.connect(self._notify_change)
        if self.on_delete:
            self.btn_del.clicked.connect(lambda: self.on_delete(self))
            
    def _notify_change(self):
        if self.on_changed:
            self.on_changed()
            
    def get_data(self):
        text = self.combo_sensor.currentText()
        parts = text.split(" - ", 1) if " - " in text else ("", text.strip())
        return {
            "kit": parts[0].strip(),
            "sensor": parts[1].strip(),
            "value": self.check_val.isChecked()
        }

class DragDropTreeWidget(QTreeWidget):
    def __init__(self, parent=None, on_dropped=None):
        super().__init__(parent)
        self.on_dropped = on_dropped
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)

    def dropEvent(self, event):
        dragged_item = self.currentItem()
        if not dragged_item:
            super().dropEvent(event)
            return

        target_item = self.itemAt(event.position().toPoint())
        if not target_item:
            parent = self.invisibleRootItem()
            index = parent.childCount()
        else:
            target_data = target_item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(target_data, dict) and "_container" in target_data:
                parent = target_item
                index = parent.childCount()
            else:
                parent = target_item.parent() or self.invisibleRootItem()
                index = parent.indexOfChild(target_item)

        current_parent = dragged_item.parent() or self.invisibleRootItem()
        current_parent.removeChild(dragged_item)
        parent.insertChild(index, dragged_item)
        self.setCurrentItem(dragged_item)
        
        event.accept()
        if self.on_dropped:
            self.on_dropped()

class AASRulesEditorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Program Deployed AAS Rules (Scratch AST)")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowMinimizeButtonHint)
        self.resize(1100, 750)
        
        self.logic_rules = {"start": [], "sequences": []}
        self.sensors_catalog = []
        self.actuators_catalog = []
        self.target_kit = ""
        
        self.apply_styles()
        self.setup_ui()
        self.load_aas_list()

    def apply_styles(self):
        self.setStyleSheet("""
            QDialog { background-color: #1e1e1e; }
            QWidget { background-color: #1e1e1e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }
            QLabel { font-size: 13px; font-weight: 500; }
            QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox, QTextEdit { 
                background-color: #2d2d2d; border: 1px solid #3d3d3d; border-radius: 4px; padding: 6px; color: white; 
            }
            QPushButton { background-color: #007acc; color: white; border: none; border-radius: 4px; padding: 8px 14px; font-weight: bold; }
            QPushButton:hover { background-color: #008be5; }
            QGroupBox { border: 1px solid #3d3d3d; border-radius: 8px; margin-top: 15px; padding-top: 20px; font-weight: bold; background-color: #252526; }
            QGroupBox[active="true"] { border: 2px solid #007acc; background-color: #2d2d30; }
            QTreeWidget, QListWidget { background-color: #252526; border: 1px solid #3d3d3d; border-radius: 4px; }
            QTreeWidget::item:selected, QListWidget::item:selected { background-color: #094771; }
            QSplitter::handle { background-color: #333; }
            QScrollArea { border: none; background-color: transparent; }
        """)

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        
        # AAS Selection top bar
        aas_layout = QHBoxLayout()
        aas_layout.addWidget(QLabel("Select Active AAS:"))
        self.combo_aas = QComboBox()
        self.combo_aas.setMinimumWidth(300)
        self.combo_aas.currentIndexChanged.connect(self.on_aas_changed)
        aas_layout.addWidget(self.combo_aas)
        
        self.btn_create_aas = QPushButton("Create New AAS")
        self.btn_create_aas.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; font-size: 12px; padding: 6px 12px;")
        self.btn_create_aas.clicked.connect(self.create_new_aas)
        aas_layout.addWidget(self.btn_create_aas)
        
        aas_layout.addStretch()
        main_layout.addLayout(aas_layout)
        
        # 3-panel UI splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.splitter)
        
        # Panel A: Left (Sequences List)
        panel_a = QGroupBox("Sequences")
        layout_a = QVBoxLayout(panel_a)
        layout_a.setContentsMargins(10, 15, 10, 10)
        self.list_sequences = QListWidget()
        self.list_sequences.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_sequences.model().rowsMoved.connect(self.on_sequences_reordered)
        self.list_sequences.addItem("Start Sequence")
        layout_a.addWidget(self.list_sequences)
        
        btn_layout_a = QHBoxLayout()
        self.btn_add_seq = QPushButton("Add Sequence")
        self.btn_del_seq = QPushButton("Delete Sequence")
        self.btn_del_seq.setStyleSheet("background-color: #a93226;")
        btn_layout_a.addWidget(self.btn_add_seq)
        btn_layout_a.addWidget(self.btn_del_seq)
        layout_a.addLayout(btn_layout_a)
        
        # Panel B: Middle (Trigger Conditions)
        self.group_triggers = QGroupBox("Trigger Conditions")
        layout_b = QVBoxLayout(self.group_triggers)
        layout_b.setContentsMargins(10, 15, 10, 10)
        
        op_layout = QHBoxLayout()
        op_layout.addWidget(QLabel("Operator:"))
        self.combo_trigger_op = QComboBox()
        self.combo_trigger_op.addItems(["AND", "OR"])
        op_layout.addWidget(self.combo_trigger_op)
        op_layout.addStretch()
        layout_b.addLayout(op_layout)
        
        self.scroll_triggers = QScrollArea()
        self.scroll_triggers.setWidgetResizable(True)
        self.triggers_container = QWidget()
        self.triggers_layout = QVBoxLayout(self.triggers_container)
        self.triggers_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.triggers_layout.setContentsMargins(0, 0, 0, 0)
        self.triggers_layout.setSpacing(5)
        self.scroll_triggers.setWidget(self.triggers_container)
        layout_b.addWidget(self.scroll_triggers)
        
        self.btn_add_trigger = QPushButton("➕ Add Trigger Condition")
        layout_b.addWidget(self.btn_add_trigger)
        
        # Panel C: Right (Sequence Steps Tree & Controls)
        self.group_steps = QGroupBox("Control Logic Steps (AST)")
        layout_c = QVBoxLayout(self.group_steps)
        layout_c.setContentsMargins(10, 15, 10, 10)
        
        self.tree_steps = DragDropTreeWidget(on_dropped=self.on_tree_steps_dropped)
        self.tree_steps.setHeaderLabels(["Step Type / Logic", "Details"])
        self.tree_steps.setColumnWidth(0, 220)
        layout_c.addWidget(self.tree_steps)
        
        # Grid of block creation buttons (Scratch-like)
        btn_add_step_layout = QGridLayout()
        self.btn_add_set_act = QPushButton("⚡ Set Actuator")
        self.btn_add_wait_time = QPushButton("⏱️ Wait Time")
        self.btn_add_wait_until = QPushButton("⏳ Wait Until")
        self.btn_add_if = QPushButton("🔀 If / Then")
        self.btn_add_if_else = QPushButton("🔀 If / Else")
        self.btn_add_while = QPushButton("🔄 While Loop")
        self.btn_add_repeat_times = QPushButton("🔁 Repeat N Times")
        self.btn_add_set_var = QPushButton("📝 Set Variable")
        self.btn_add_call_http = QPushButton("📞 Call HTTP")

        btn_add_step_layout.addWidget(self.btn_add_set_act, 0, 0)
        btn_add_step_layout.addWidget(self.btn_add_wait_time, 0, 1)
        btn_add_step_layout.addWidget(self.btn_add_wait_until, 0, 2)
        btn_add_step_layout.addWidget(self.btn_add_if, 0, 3)
        btn_add_step_layout.addWidget(self.btn_add_if_else, 1, 0)
        btn_add_step_layout.addWidget(self.btn_add_while, 1, 1)
        btn_add_step_layout.addWidget(self.btn_add_repeat_times, 1, 2)
        btn_add_step_layout.addWidget(self.btn_add_set_var, 1, 3)
        btn_add_step_layout.addWidget(self.btn_add_call_http, 2, 0)
        layout_c.addLayout(btn_add_step_layout)
        
        btn_control_layout = QHBoxLayout()
        self.btn_delete_step = QPushButton("Delete Step")
        self.btn_delete_step.setStyleSheet("background-color: #a93226;")
        btn_control_layout.addWidget(self.btn_delete_step)
        layout_c.addLayout(btn_control_layout)
        
        # Bottom Edit Pane
        self.group_step_edit = QGroupBox("Edit Selected Block")
        self.step_edit_layout = QVBoxLayout(self.group_step_edit)
        self.step_edit_layout.setContentsMargins(10, 15, 10, 10)
        self.step_edit_stack = QStackedWidget()
        self.step_edit_layout.addWidget(self.step_edit_stack)
        layout_c.addWidget(self.group_step_edit)
        
        # Add to splitter
        self.splitter.addWidget(panel_a)
        self.splitter.addWidget(self.group_triggers)
        self.splitter.addWidget(self.group_steps)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 3)
        
        # Stack Pages
        # 0: Empty
        page_empty = QWidget()
        layout_empty = QVBoxLayout(page_empty)
        lbl_empty = QLabel("Select a step or block in the tree to edit")
        lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout_empty.addWidget(lbl_empty)
        self.step_edit_stack.addWidget(page_empty)
        
        # 1: set_actuator
        page_actuator = QWidget()
        layout_actuator = QFormLayout(page_actuator)
        layout_actuator.setContentsMargins(0, 0, 0, 0)
        self.edit_step_actuator_combo = QComboBox()
        self.edit_step_actuator_val = QCheckBox("Active / On")
        layout_actuator.addRow("Actuator:", self.edit_step_actuator_combo)
        layout_actuator.addRow("Target State:", self.edit_step_actuator_val)
        self.step_edit_stack.addWidget(page_actuator)
        
        # 2: wait_time
        page_time = QWidget()
        layout_time = QFormLayout(page_time)
        layout_time.setContentsMargins(0, 0, 0, 0)
        self.edit_step_wait_time_spin = QDoubleSpinBox()
        self.edit_step_wait_time_spin.setRange(0.0, 3600.0)
        self.edit_step_wait_time_spin.setSingleStep(0.1)
        self.edit_step_wait_time_spin.setSuffix(" seconds")
        layout_time.addRow("Delay:", self.edit_step_wait_time_spin)
        self.step_edit_stack.addWidget(page_time)
        
        # 3: Condition Editor (used for wait_until, if, while)
        page_cond = QWidget()
        layout_cond = QFormLayout(page_cond)
        layout_cond.setContentsMargins(0, 0, 0, 0)
        self.edit_cond_sensor_combo = QComboBox()
        self.edit_cond_val_combo = QComboBox()
        self.edit_cond_val_combo.addItems(["True (Active)", "False (Inactive)"])
        self.edit_cond_timeout_spin = QDoubleSpinBox()
        self.edit_cond_timeout_spin.setRange(0.1, 3600.0)
        self.edit_cond_timeout_spin.setValue(15.0)
        self.edit_cond_timeout_spin.setSuffix(" seconds")
        self.lbl_cond_timeout = QLabel("Timeout:")
        layout_cond.addRow("Sensor:", self.edit_cond_sensor_combo)
        layout_cond.addRow("Target Condition State:", self.edit_cond_val_combo)
        layout_cond.addRow(self.lbl_cond_timeout, self.edit_cond_timeout_spin)
        self.step_edit_stack.addWidget(page_cond)

        # 4: repeat_times
        page_repeat = QWidget()
        layout_repeat = QFormLayout(page_repeat)
        layout_repeat.setContentsMargins(0, 0, 0, 0)
        self.edit_repeat_times_spin = QSpinBox()
        self.edit_repeat_times_spin.setRange(1, 9999)
        layout_repeat.addRow("Repeat Count:", self.edit_repeat_times_spin)
        self.step_edit_stack.addWidget(page_repeat)

        # 5: set_variable
        page_var = QWidget()
        layout_var = QFormLayout(page_var)
        layout_var.setContentsMargins(0, 0, 0, 0)
        self.edit_var_name = QLineEdit()
        self.edit_var_value = QLineEdit()
        layout_var.addRow("Variable Name:", self.edit_var_name)
        layout_var.addRow("Value:", self.edit_var_value)
        self.step_edit_stack.addWidget(page_var)

        # 6: call_http_service
        page_http = QWidget()
        layout_http = QFormLayout(page_http)
        layout_http.setContentsMargins(0, 0, 0, 0)
        self.edit_http_url = QLineEdit()
        self.edit_http_url.setPlaceholderText("http://localhost:8080/api/do-something")
        self.combo_http_method = QComboBox()
        self.combo_http_method.addItems(["POST", "GET", "PUT"])
        self.edit_http_payload = QTextEdit()
        self.edit_http_payload.setPlaceholderText('{"key": "value"}')
        self.edit_http_payload.setMaximumHeight(80)
        layout_http.addRow("URL/Endpoint:", self.edit_http_url)
        layout_http.addRow("Method:", self.combo_http_method)
        layout_http.addRow("Payload (JSON):", self.edit_http_payload)
        self.step_edit_stack.addWidget(page_http)
        
        # Bottom save/close bar
        btn_bar = QHBoxLayout()
        self.btn_save = QPushButton("Save & Deploy Rules (AAS)")
        self.btn_save.setMinimumHeight(40)
        self.btn_save.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
        self.btn_save.clicked.connect(self.save_and_deploy)
        
        self.btn_close = QPushButton("Close")
        self.btn_close.setMinimumHeight(40)
        self.btn_close.clicked.connect(self.reject)
        
        btn_bar.addWidget(self.btn_save)
        btn_bar.addWidget(self.btn_close)
        main_layout.addLayout(btn_bar)
        
        # Connections
        self.list_sequences.currentRowChanged.connect(self.on_sequence_selected)
        self.btn_add_seq.clicked.connect(self.add_sequence)
        self.btn_del_seq.clicked.connect(self.delete_sequence)
        
        self.combo_trigger_op.currentTextChanged.connect(self.save_trigger_changes)
        self.btn_add_trigger.clicked.connect(self.add_trigger_condition)
        
        self.tree_steps.itemSelectionChanged.connect(self.update_step_edit_pane)
        self.btn_add_set_act.clicked.connect(lambda: self.add_step_node("set_actuator"))
        self.btn_add_wait_time.clicked.connect(lambda: self.add_step_node("wait_time"))
        self.btn_add_wait_until.clicked.connect(lambda: self.add_step_node("wait_until"))
        self.btn_add_if.clicked.connect(lambda: self.add_step_node("if"))
        self.btn_add_if_else.clicked.connect(lambda: self.add_step_node("if_else"))
        self.btn_add_while.clicked.connect(lambda: self.add_step_node("while"))
        self.btn_add_repeat_times.clicked.connect(lambda: self.add_step_node("repeat_times"))
        self.btn_add_set_var.clicked.connect(lambda: self.add_step_node("set_variable"))
        self.btn_add_call_http.clicked.connect(lambda: self.add_step_node("call_http_service"))
        self.btn_delete_step.clicked.connect(self.delete_step_node)
        self.btn_del_seq.setEnabled(False)
        self.step_edit_stack.setCurrentIndex(0)

        # Connect edit widgets to auto-save on change
        self.edit_step_actuator_combo.currentTextChanged.connect(self.save_current_step_changes)
        self.edit_step_actuator_val.toggled.connect(self.save_current_step_changes)
        self.edit_step_wait_time_spin.valueChanged.connect(self.save_current_step_changes)
        self.edit_cond_sensor_combo.currentTextChanged.connect(self.save_current_step_changes)
        self.edit_cond_val_combo.currentIndexChanged.connect(self.save_current_step_changes)
        self.edit_cond_timeout_spin.valueChanged.connect(self.save_current_step_changes)
        self.edit_repeat_times_spin.valueChanged.connect(self.save_current_step_changes)
        self.edit_var_name.textChanged.connect(self.save_current_step_changes)
        self.edit_var_value.textChanged.connect(self.save_current_step_changes)
        self.edit_http_url.textChanged.connect(self.save_current_step_changes)
        self.combo_http_method.currentTextChanged.connect(self.save_current_step_changes)
        self.edit_http_payload.textChanged.connect(self.save_current_step_changes)

    def create_new_aas(self):
        id_short, ok1 = QInputDialog.getText(self, "Create New AAS", "Enter AAS Name (idShort):", QLineEdit.EchoMode.Normal, "NewResource")
        if not ok1 or not id_short.strip(): return
        id_short = id_short.strip()
        default_id = f"https://acplt.org/AAS_{sanitize_aas_id(id_short)}"
        shell_id, ok2 = QInputDialog.getText(self, "Create New AAS", "Enter AAS Identifier (ID):", QLineEdit.EchoMode.Normal, default_id)
        if not ok2 or not shell_id.strip(): return
        shell_id = shell_id.strip()
        
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            shell_payload = {
                "id": shell_id, "idShort": id_short, "modelType": "AssetAdministrationShell",
                "assetInformation": {"assetKind": "Instance", "globalAssetId": f"http://acplt.org/Assets/{sanitize_aas_id(id_short)}"},
                "submodels": []
            }
            res_shell = requests.post("http://localhost:8081/shells", json=shell_payload, headers={"Content-Type": "application/json"}, timeout=5)
            if res_shell.status_code not in [200, 201]:
                if res_shell.status_code == 409: raise Exception("An AAS with this ID already exists.")
                raise Exception(f"Failed shell creation: {res_shell.text}")
                
            sm_id = f"https://acplt.org/Submodels/ControlLogic_{sanitize_aas_id(id_short)}"
            submodel_payload = {
                "id": sm_id, "idShort": "ControlLogic", "modelType": "Submodel",
                "submodelElements": [{"idShort": "Rules", "modelType": "Property", "value": json.dumps({"start": [], "sequences": []}), "valueType": "xs:string"}]
            }
            res_sm = requests.post("http://localhost:8081/submodels", json=submodel_payload, headers={"Content-Type": "application/json"}, timeout=5)
            if res_sm.status_code not in [200, 201]:
                requests.delete(f"http://localhost:8081/shells/{encode_id(shell_id)}", timeout=5)
                raise Exception(f"Failed submodel creation: {res_sm.text}")
                
            link_payload = {"type": "ModelReference", "keys": [{"type": "Submodel", "value": sm_id}]}
            res_link = requests.post(f"http://localhost:8081/shells/{encode_id(shell_id)}/submodel-refs", json=link_payload, headers={"Content-Type": "application/json"}, timeout=5)
            if res_link.status_code not in [200, 201, 204]:
                requests.delete(f"http://localhost:8081/submodels/{encode_id(sm_id)}", timeout=5)
                requests.delete(f"http://localhost:8081/shells/{encode_id(shell_id)}", timeout=5)
                raise Exception(f"Failed linking submodel: {res_link.text}")
                
            time.sleep(0.5)
            while QApplication.overrideCursor() is not None: QApplication.restoreOverrideCursor()
            QMessageBox.information(self, "Success", f"AAS '{id_short}' created!")
            self.load_aas_list()
            for index in range(self.combo_aas.count()):
                data = self.combo_aas.itemData(index)
                if data and data.get("id") == shell_id:
                    self.combo_aas.setCurrentIndex(index); break
        except Exception as e:
            while QApplication.overrideCursor() is not None: QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Error creating AAS: {e}")

    def load_aas_list(self):
        self.combo_aas.blockSignals(True); self.combo_aas.clear()
        try:
            res = requests.get("http://localhost:8082/shell-descriptors", timeout=5)
            if res.status_code == 200:
                shells = res.json().get("result", [])
                for s in shells: self.combo_aas.addItem(f"{s.get('idShort')} ({s.get('id')})", s)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not connect to registry: {e}")
        self.combo_aas.blockSignals(False)
        if self.combo_aas.count() > 0:
            self.combo_aas.setCurrentIndex(0); self.on_aas_changed()

    def get_current_kit(self): return self.target_kit

    def build_catalog(self):
        self.sensors_catalog = []; self.actuators_catalog = []
        try:
            res = requests.get("http://localhost:8082/shell-descriptors", timeout=5)
            if res.status_code != 200: return
            shells = res.json().get("result", [])
            act_types = ["Conveyor", "RotateContinuous", "TranslateContinuous", "Piston", "Linear"]
            sens_types = ["TriggerZone", "Presence", "Proximity"]
            for s in shells:
                shell_id = s.get("id"); kit_name = s.get("idShort", "AAS")
                encoded_shell_id = encode_id(shell_id)
                try:
                    shell_res = requests.get(f"http://localhost:8081/shells/{encoded_shell_id}", timeout=2)
                    if shell_res.status_code != 200: continue
                    submodel_refs = shell_res.json().get("submodels", [])
                    for ref in submodel_refs:
                        keys = ref.get("keys", [])
                        if not keys: continue
                        sm_id = keys[0].get("value")
                        sm_res = requests.get(f"http://localhost:8081/submodels/{encode_id(sm_id)}", timeout=2)
                        if sm_res.status_code != 200: continue
                        sm_data = sm_res.json()
                        id_short = sm_data.get("idShort", "")
                        if "BehaviorMapping" in id_short or "Behavior" in id_short:
                            for el in sm_data.get("submodelElements", []):
                                if el["modelType"] == "SubmodelElementCollection":
                                    props = {p["idShort"]: p.get("value") for p in el.get("value", []) if p["modelType"] == "Property"}
                                    b_type = props.get("Type", ""); pin_id = props.get("PinID", "")
                                    if pin_id:
                                        item_str = f"{kit_name} - {pin_id}"
                                        if b_type in act_types: self.actuators_catalog.append(item_str)
                                        elif b_type in sens_types: self.sensors_catalog.append(item_str)
                except Exception as e: print(f"Error fetching catalog: {e}")
        except Exception as e: print(f"Error building catalog: {e}")

    def on_aas_changed(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.build_catalog()
            item_data = self.combo_aas.currentData()
            if not item_data:
                self.logic_rules = {"start": [], "sequences": []}
                self.list_sequences.clear(); self.tree_steps.clear(); return
            
            shell_id = item_data.get("id"); kit_name = item_data.get("idShort", "AAS")
            self.target_kit = kit_name
            shell_res = requests.get(f"http://localhost:8081/shells/{encode_id(shell_id)}", timeout=5)
            self.logic_rules = {"start": [], "sequences": []}
            if shell_res.status_code == 200:
                submodel_refs = shell_res.json().get("submodels", [])
                for ref in submodel_refs:
                    keys = ref.get("keys", [])
                    if keys:
                        sm_id = keys[0].get("value")
                        if "ControlLogic" in sm_id or "Logic" in sm_id:
                            sm_res = requests.get(f"http://localhost:8081/submodels/{encode_id(sm_id)}", timeout=5)
                            if sm_res.status_code == 200:
                                for el in sm_res.json().get("submodelElements", []):
                                    if el["idShort"] == "Rules":
                                        val = el.get("value", "")
                                        if val:
                                            try: self.logic_rules = json.loads(val)
                                            except Exception: traceback.print_exc()
                                                
            self.list_sequences.blockSignals(True); self.list_sequences.clear()
            self.list_sequences.addItem("Start Sequence")
            for seq in self.logic_rules.get("sequences", []):
                self.list_sequences.addItem(seq.get("name", "Custom Sequence"))
            self.list_sequences.setCurrentRow(0); self.list_sequences.blockSignals(False)
            self.on_sequence_selected(0)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load rules: {e}")
        finally:
            while QApplication.overrideCursor() is not None: QApplication.restoreOverrideCursor()

    def save_trigger_changes(self):
        row = self.list_sequences.currentRow()
        if row <= 0: return
        seq_idx = row - 1
        if seq_idx < 0 or seq_idx >= len(self.logic_rules["sequences"]): return
        seq = self.logic_rules["sequences"][seq_idx]
        if "trigger" not in seq or not isinstance(seq["trigger"], dict):
            seq["trigger"] = {"operator": "AND", "conditions": []}
        seq["trigger"]["operator"] = self.combo_trigger_op.currentText()
        triggers = []
        for i in range(self.triggers_layout.count()):
            w = self.triggers_layout.itemAt(i).widget()
            if isinstance(w, TriggerRow): triggers.append(w.get_data())
        seq["trigger"]["conditions"] = triggers

    def add_trigger_condition(self):
        row = self.list_sequences.currentRow()
        if row <= 0: return
        rule_options = [f"[Rule] {s['name']}" for s in self.logic_rules.get("sequences", [])]
        combined_options = self.sensors_catalog + rule_options
        row_widget = TriggerRow(
            self, combined_options, initial_sensor="", initial_value=True,
            on_delete=lambda w=None: self.delete_trigger_condition(row_widget),
            on_changed=self.save_trigger_changes
        )
        self.triggers_layout.addWidget(row_widget)
        self.save_trigger_changes()

    def delete_trigger_condition(self, row_widget):
        row_widget.deleteLater()
        QTimer.singleShot(50, self.save_trigger_changes)

    def on_sequence_selected(self, row):
        if row == 0:
            self.group_triggers.setVisible(False)
            self.refresh_steps_tree()
        elif row > 0 and (row - 1) < len(self.logic_rules["sequences"]):
            self.group_triggers.setVisible(True)
            seq = self.logic_rules["sequences"][row - 1]
            if "trigger" not in seq or not isinstance(seq["trigger"], dict):
                seq["trigger"] = {"operator": "AND", "conditions": []}
            self.combo_trigger_op.blockSignals(True)
            self.combo_trigger_op.setCurrentText(seq["trigger"].get("operator", "AND"))
            self.combo_trigger_op.blockSignals(False)
            
            while self.triggers_layout.count() > 0:
                child = self.triggers_layout.takeAt(0)
                if child.widget(): child.widget().deleteLater()
                    
            rule_options = [f"[Rule] {s['name']}" for s in self.logic_rules.get("sequences", [])]
            combined_options = self.sensors_catalog + rule_options

            for cond in seq["trigger"].get("conditions", []):
                initial_sens = f"[Rule] {cond['sequence']}" if "sequence" in cond else cond.get("sensor", "")
                initial_kit = None if "sequence" in cond else cond.get("kit")
                row_widget = TriggerRow(
                    self, combined_options, initial_sensor=initial_sens,
                    initial_value=cond.get("value", True),
                    on_delete=lambda w=None: self.delete_trigger_condition(row_widget),
                    on_changed=self.save_trigger_changes, initial_kit=initial_kit
                )
                self.triggers_layout.addWidget(row_widget)
            self.refresh_steps_tree()
        else:
            self.group_triggers.setVisible(False)
            self.tree_steps.clear()
            self.step_edit_stack.setCurrentIndex(0)
        self.btn_del_seq.setEnabled(row > 0)

    def get_current_steps_list(self):
        row = self.list_sequences.currentRow()
        if row == 0:
            return self.logic_rules["start"]
        elif row > 0 and (row - 1) < len(self.logic_rules["sequences"]):
            return self.logic_rules["sequences"][row - 1]["steps"]
        return None


    def add_sequence(self):
        name, ok = QInputDialog.getText(self, "Add Sequence", "Enter sequence name:")
        if ok and name.strip():
            name = name.strip()
            existing = ["Start Sequence"] + [s["name"] for s in self.logic_rules["sequences"]]
            if name in existing:
                QMessageBox.warning(self, "Warning", "A sequence with this name already exists.")
                return
            self.logic_rules["sequences"].append({
                "name": name, "trigger": {"operator": "AND", "conditions": []}, "steps": []
            })
            self.list_sequences.addItem(name)
    def add_step_node(self, step_type):
        steps = self.get_current_steps_list()
        if steps is None: return

        selected_item = self.tree_steps.currentItem()
        target_list = steps

        if selected_item:
            data = selected_item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, dict):
                if "_container" in data:
                    container = data["_container"]
                    parent_id = data.get("_parent_id")
                    
                    # Search tree for step matching parent_id in steps hierarchy
                    def find_step_by_id(current_list, target_id):
                        if not isinstance(current_list, list): return None
                        for s in current_list:
                            if isinstance(s, dict):
                                if s.get("_id") == target_id: return s
                                for sub in ["then", "else", "body"]:
                                    res = find_step_by_id(s.get(sub, []), target_id)
                                    if res: return res
                        return None
                        
                    parent_step = find_step_by_id(steps, parent_id)
                    if parent_step:
                        if container not in parent_step or not isinstance(parent_step[container], list):
                            parent_step[container] = []
                        target_list = parent_step[container]

                elif data.get("type") in ["if", "if_else"]:
                    target_id = data.get("_id")
                    def find_step_by_id(current_list, tid):
                        if not isinstance(current_list, list): return None
                        for s in current_list:
                            if isinstance(s, dict):
                                if s.get("_id") == tid: return s
                                for sub in ["then", "else", "body"]:
                                    res = find_step_by_id(s.get(sub, []), tid)
                                    if res: return res
                        return None
                    target_step = find_step_by_id(steps, target_id)
                    if target_step:
                        if "then" not in target_step or not isinstance(target_step["then"], list):
                            target_step["then"] = []
                        target_list = target_step["then"]

                elif data.get("type") in ["while", "repeat_while", "repeat_times"]:
                    target_id = data.get("_id")
                    def find_step_by_id(current_list, tid):
                        if not isinstance(current_list, list): return None
                        for s in current_list:
                            if isinstance(s, dict):
                                if s.get("_id") == tid: return s
                                for sub in ["then", "else", "body"]:
                                    res = find_step_by_id(s.get(sub, []), tid)
                                    if res: return res
                        return None
                    target_step = find_step_by_id(steps, target_id)
                    if target_step:
                        if "body" not in target_step or not isinstance(target_step["body"], list):
                            target_step["body"] = []
                        target_list = target_step["body"]

        import uuid
        act_text = self.actuators_catalog[0] if self.actuators_catalog else "Motor_1"
        kit, act = (act_text.split(" - ", 1)) if " - " in act_text else (self.get_current_kit(), act_text)
        sens_text = self.sensors_catalog[0] if self.sensors_catalog else "Sensor_1"
        sens_kit, sens = (sens_text.split(" - ", 1)) if " - " in sens_text else (self.get_current_kit(), sens_text)
 
        new_step = {"_id": str(uuid.uuid4())}
        if step_type == "set_actuator":
            new_step.update({"type": "set_actuator", "kit": kit, "actuator": act, "value": False})
        elif step_type == "wait_time":
            new_step.update({"type": "wait_time", "seconds": 1.0})
        elif step_type == "wait_until":
            new_step.update({"type": "wait_until", "condition": {"sensor": sens, "value": True, "kit": sens_kit}})
        elif step_type in ["if", "if_else"]:
            new_step.update({
                "type": step_type,
                "condition": {"sensor": sens, "value": True, "kit": sens_kit},
                "then": []
            })
            if step_type == "if_else": new_step["else"] = []
        elif step_type == "while":
            new_step.update({"type": "while", "condition": {"sensor": sens, "value": True, "kit": sens_kit}, "body": []})
        elif step_type == "repeat_times":
            new_step.update({"type": "repeat_times", "times": 5, "body": []})
        elif step_type == "set_variable":
            new_step.update({"type": "set_variable", "variable": "counter", "value": 0})
        elif step_type == "call_http_service":
            new_step.update({"type": "call_http_service", "url": "http://localhost:8080/api/skill", "method": "POST", "payload": "{}"})

        target_list.append(new_step)
        self.refresh_steps_tree()

    def keyPressEvent(self, event):
        if event.key() in [Qt.Key.Key_Delete, Qt.Key.Key_Backspace]:
            if self.tree_steps.hasFocus():
                self.delete_step_node()
                return
        super().keyPressEvent(event)

    def delete_step_node(self):
        item = self.tree_steps.currentItem()
        if not item: return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        
        target_id = None
        if isinstance(data, dict):
            if "_container" in data:
                target_id = data.get("_parent_id")
            else:
                target_id = data.get("_id")

        if not target_id: return

        steps = self.get_current_steps_list()
        def remove_recursive(current_list, tid):
            if not isinstance(current_list, list): return False
            for elem in list(current_list):
                if isinstance(elem, dict):
                    if elem.get("_id") == tid:
                        current_list.remove(elem)
                        return True
                    for sub in ["then", "else", "body"]:
                        if sub in elem and remove_recursive(elem[sub], tid):
                            return True
            return False

        if remove_recursive(steps, target_id):
            self.refresh_steps_tree()

    def update_step_edit_pane(self):
        item = self.tree_steps.currentItem()
        if not item:
            self.step_edit_stack.setCurrentIndex(0); return

        step = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(step, dict) or "_container" in step:
            self.step_edit_stack.setCurrentIndex(0); return

        t = step.get("type")
        self.block_step_edit_signals(True)

        if t == "set_actuator":
            self.step_edit_stack.setCurrentIndex(1)
            self.edit_step_actuator_combo.clear(); self.edit_step_actuator_combo.addItems(self.actuators_catalog)
            target_act = step.get("actuator", "")
            for i in range(self.edit_step_actuator_combo.count()):
                if self.edit_step_actuator_combo.itemText(i).endswith(target_act):
                    self.edit_step_actuator_combo.setCurrentIndex(i); break
            self.edit_step_actuator_val.setChecked(bool(step.get("value", False)))

        elif t == "wait_time":
            self.step_edit_stack.setCurrentIndex(2)
            self.edit_step_wait_time_spin.setValue(float(step.get("seconds", 0.0)))

        elif t in ["wait_until", "if", "if_else", "while"]:
            self.step_edit_stack.setCurrentIndex(3)
            is_wait_until = (t == "wait_until")
            self.lbl_cond_timeout.setVisible(is_wait_until)
            self.edit_cond_timeout_spin.setVisible(is_wait_until)
            if is_wait_until:
                self.edit_cond_timeout_spin.setValue(float(step.get("timeout", 15.0)))
            self.edit_cond_sensor_combo.clear(); self.edit_cond_sensor_combo.addItems(self.sensors_catalog)
            target_sens = step.get("condition", {}).get("sensor", "")
            for i in range(self.edit_cond_sensor_combo.count()):
                if self.edit_cond_sensor_combo.itemText(i).endswith(target_sens):
                    self.edit_cond_sensor_combo.setCurrentIndex(i); break
            cond_val = bool(step.get("condition", {}).get("value", True))
            self.edit_cond_val_combo.setCurrentIndex(0 if cond_val else 1)

        elif t == "repeat_times":
            self.step_edit_stack.setCurrentIndex(4)
            self.edit_repeat_times_spin.setValue(int(step.get("times", 1)))

        elif t == "set_variable":
            self.step_edit_stack.setCurrentIndex(5)
            self.edit_var_name.setText(str(step.get("variable", "")))
            self.edit_var_value.setText(str(step.get("value", "")))

        elif t == "call_http_service":
            self.step_edit_stack.setCurrentIndex(6)
            self.edit_http_url.setText(str(step.get("url", "")))
            self.combo_http_method.setCurrentText(str(step.get("method", "POST")))
            self.edit_http_payload.setPlainText(str(step.get("payload", "")))

        else:
            self.step_edit_stack.setCurrentIndex(0)

        self.block_step_edit_signals(False)

    def block_step_edit_signals(self, b):
        self.edit_step_actuator_combo.blockSignals(b)
        self.edit_step_actuator_val.blockSignals(b)
        self.edit_step_wait_time_spin.blockSignals(b)
        self.edit_cond_sensor_combo.blockSignals(b)
        self.edit_cond_val_combo.blockSignals(b)
        self.edit_repeat_times_spin.blockSignals(b)
        self.edit_var_name.blockSignals(b)
        self.edit_var_value.blockSignals(b)
        self.edit_http_url.blockSignals(b)
        self.combo_http_method.blockSignals(b)
        self.edit_http_payload.blockSignals(b)

    def save_current_step_changes(self):
        item = self.tree_steps.currentItem()
        if not item: return
        step_data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(step_data, dict) or "_container" in step_data: return
        
        target_id = step_data.get("_id")
        steps = self.get_current_steps_list()
        def find_step_by_id(current_list, tid):
            if not isinstance(current_list, list): return None
            for s in current_list:
                if isinstance(s, dict):
                    if s.get("_id") == tid: return s
                    for sub in ["then", "else", "body"]:
                        res = find_step_by_id(s.get(sub, []), tid)
                        if res: return res
            return None
            
        step = find_step_by_id(steps, target_id)
        if not step: return

        t = step.get("type")

        if t == "set_actuator":
            act_text = self.edit_step_actuator_combo.currentText()
            kit_part, act = (act_text.split(" - ", 1)) if " - " in act_text else ("", act_text)
            step["kit"] = kit_part
            step["actuator"] = act
            step["value"] = self.edit_step_actuator_val.isChecked()
            
        elif t == "wait_time":
            step["seconds"] = self.edit_step_wait_time_spin.value()
            
        elif t in ["wait_until", "if", "if_else", "while"]:
            sens_text = self.edit_cond_sensor_combo.currentText()
            kit_part, sens = (sens_text.split(" - ", 1)) if " - " in sens_text else ("", sens_text)
            is_true = (self.edit_cond_val_combo.currentIndex() == 0)
            step["condition"] = {"sensor": sens, "value": is_true, "kit": kit_part}
            if t == "wait_until":
                step["timeout"] = self.edit_cond_timeout_spin.value()
            
        elif t == "repeat_times":
            step["times"] = self.edit_repeat_times_spin.value()

        elif t == "set_variable":
            step["variable"] = self.edit_var_name.text().strip()
            val_str = self.edit_var_value.text().strip()
            if val_str.isdigit(): step["value"] = int(val_str)
            else:
                try: step["value"] = float(val_str)
                except: step["value"] = val_str

        elif t == "call_http_service":
            step["url"] = self.edit_http_url.text().strip()
            step["method"] = self.combo_http_method.currentText()
            step["payload"] = self.edit_http_payload.toPlainText().strip()

        if t == "set_actuator":
            item.setText(1, f"[{step.get('kit', '')}] {step.get('actuator', '')} -> {step.get('value')}")
        elif t == "wait_time":
            item.setText(1, f"{step.get('seconds', 0.0)}s")
        elif t in ["wait_until", "if", "if_else", "while"]:
            cond = step.get("condition", {})
            if t == "wait_until":
                item.setText(1, f"({cond.get('sensor', '')} == {cond.get('value')}) (timeout: {step.get('timeout', 15.0)}s)")
            else:
                item.setText(1, f"({cond.get('sensor', '')} == {cond.get('value')})")
        elif t == "repeat_times":
            item.setText(1, f"Count: {step.get('times', 1)}")
        elif t == "set_variable":
            item.setText(1, f"{step.get('variable', '')} = {step.get('value', '')}")
        elif t == "call_http_service":
            item.setText(1, f"{step.get('method', 'POST')} -> {step.get('url', '')}")

    def get_current_steps_list(self):
        row = self.list_sequences.currentRow()
        if row == 0: return self.logic_rules["start"]
        elif row > 0 and (row - 1) < len(self.logic_rules["sequences"]):
            return self.logic_rules["sequences"][row - 1]["steps"]
        return None

    def refresh_steps_tree(self):
        self.tree_steps.blockSignals(True)
        self.tree_steps.clear()
        steps = self.get_current_steps_list()
        if steps is not None:
            for step in steps:
                self.build_tree_node(self.tree_steps.invisibleRootItem(), step)
        self.tree_steps.expandAll()
        self.tree_steps.blockSignals(False)
        self.update_step_edit_pane()

    def build_tree_node(self, parent_item, step):
        import uuid
        if "_id" not in step: step["_id"] = str(uuid.uuid4())
        t = step.get("type")
        node = QTreeWidgetItem(parent_item)
        node.setData(0, Qt.ItemDataRole.UserRole, step)
        node.setFlags(node.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        
        if t == "set_actuator":
            kit = step.get("kit", "")
            act = step.get("actuator", "None")
            val = step.get("value", False)
            node.setText(0, "⚡ Set Actuator")
            node.setText(1, f"[{kit}] {act} -> {val}")
        elif t == "wait_time":
            val = step.get("seconds", 0.0)
            node.setText(0, "⏱️ Wait Time")
            node.setText(1, f"{val}s")
        elif t == "wait_until":
            cond = step.get("condition", {})
            sens = cond.get("sensor", "Sensor")
            val = cond.get("value", True)
            timeout = step.get("timeout", 15.0)
            node.setText(0, "⏳ Wait Until")
            node.setText(1, f"Sensor '{sens}' == {val} (timeout: {timeout}s)")
        elif t in ["if", "if_else"]:
            cond = step.get("condition", {})
            sens = cond.get("sensor", "Sensor")
            val = cond.get("value", True)
            node.setText(0, "🔀 If Block")
            node.setText(1, f"If ({sens} == {val})")
            
            then_node = QTreeWidgetItem(node)
            then_node.setText(0, "🟢 Then")
            then_node.setData(0, Qt.ItemDataRole.UserRole, {"_container": "then", "_parent_id": step["_id"]})
            then_node.setFlags((then_node.flags() & ~Qt.ItemFlag.ItemIsDragEnabled) | Qt.ItemFlag.ItemIsDropEnabled)
            for s in step.get("then", []): self.build_tree_node(then_node, s)
            
            if t == "if_else" or "else" in step:
                else_node = QTreeWidgetItem(node)
                else_node.setText(0, "🔴 Else")
                else_node.setData(0, Qt.ItemDataRole.UserRole, {"_container": "else", "_parent_id": step["_id"]})
                else_node.setFlags((else_node.flags() & ~Qt.ItemFlag.ItemIsDragEnabled) | Qt.ItemFlag.ItemIsDropEnabled)
                for s in step.get("else", []): self.build_tree_node(else_node, s)
        elif t in ["while", "repeat_while"]:
            cond = step.get("condition", {})
            sens = cond.get("sensor", "Sensor")
            val = cond.get("value", True)
            node.setText(0, "🔄 While Loop")
            node.setText(1, f"While ({sens} == {val})")
            
            body_node = QTreeWidgetItem(node)
            body_node.setText(0, "🔁 Body")
            body_node.setData(0, Qt.ItemDataRole.UserRole, {"_container": "body", "_parent_id": step["_id"]})
            body_node.setFlags((body_node.flags() & ~Qt.ItemFlag.ItemIsDragEnabled) | Qt.ItemFlag.ItemIsDropEnabled)
            for s in step.get("body", []): self.build_tree_node(body_node, s)
        elif t == "repeat_times":
            times = step.get("times", 1)
            node.setText(0, "🔁 Repeat N Times")
            node.setText(1, f"Count: {times}")
            body_node = QTreeWidgetItem(node)
            body_node.setText(0, "🔁 Body")
            body_node.setData(0, Qt.ItemDataRole.UserRole, {"_container": "body", "_parent_id": step["_id"]})
            body_node.setFlags((body_node.flags() & ~Qt.ItemFlag.ItemIsDragEnabled) | Qt.ItemFlag.ItemIsDropEnabled)
            for s in step.get("body", []): self.build_tree_node(body_node, s)
        elif t == "set_variable":
            vname = step.get("variable", "var")
            vval = step.get("value", "")
            node.setText(0, "📝 Set Variable")
            node.setText(1, f"{vname} = {vval}")
        elif t == "call_http_service":
            method = step.get("method", "POST")
            url = step.get("url", "")
            node.setText(0, "📞 Call HTTP Service")
            node.setText(1, f"{method} -> {url}")

    def delete_sequence(self):
        row = self.list_sequences.currentRow()
        if row <= 0:
            QMessageBox.warning(self, "Warning", "Cannot delete the permanent Start Sequence.")
            return
        seq_idx = row - 1
        reply = QMessageBox.question(
            self, "Confirm Delete", f"Delete sequence '{self.logic_rules['sequences'][seq_idx]['name']}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            del self.logic_rules["sequences"][seq_idx]
            self.list_sequences.takeItem(row)
            self.list_sequences.setCurrentRow(row - 1)

    def on_sequences_reordered(self, parent, start, end, destination, row):
        new_sequences = []
        for i in range(self.list_sequences.count()):
            item = self.list_sequences.item(i)
            name = item.text()
            if name == "Start Sequence":
                if i != 0:
                    self.list_sequences.blockSignals(True)
                    self.list_sequences.takeItem(i)
                    self.list_sequences.insertItem(0, item)
                    self.list_sequences.setCurrentRow(0)
                    self.list_sequences.blockSignals(False)
                continue
            for seq in self.logic_rules.get("sequences", []):
                if seq.get("name") == name:
                    new_sequences.append(seq)
                    break
        self.logic_rules["sequences"] = new_sequences
        self.save_trigger_changes()

    def on_tree_steps_dropped(self):
        steps = self.rebuild_steps_from_tree(self.tree_steps.invisibleRootItem())
        row = self.list_sequences.currentRow()
        if row == 0:
            self.logic_rules["start"] = steps
        elif row > 0 and (row - 1) < len(self.logic_rules["sequences"]):
            self.logic_rules["sequences"][row - 1]["steps"] = steps
        self.save_trigger_changes()
        self.refresh_steps_tree()

    def rebuild_steps_from_tree(self, parent_item):
        steps_list = []
        for i in range(parent_item.childCount()):
            child = parent_item.child(i)
            data = child.data(0, Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                continue
            if "_container" in data:
                continue
            
            step = data.copy()
            t = step.get("type")
            
            if t in ["if", "if_else"]:
                step["then"] = []
                if t == "if_else":
                    step["else"] = []
                for j in range(child.childCount()):
                    sub_child = child.child(j)
                    sub_data = sub_child.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(sub_data, dict) and "_container" in sub_data:
                        container = sub_data["_container"]
                        if container == "then":
                            step["then"] = self.rebuild_steps_from_tree(sub_child)
                        elif container == "else" and t == "if_else":
                            step["else"] = self.rebuild_steps_from_tree(sub_child)
            elif t in ["while", "repeat_while", "repeat_times"]:
                step["body"] = []
                for j in range(child.childCount()):
                    sub_child = child.child(j)
                    sub_data = sub_child.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(sub_data, dict) and "_container" in sub_data:
                        container = sub_data["_container"]
                        if container == "body":
                            step["body"] = self.rebuild_steps_from_tree(sub_child)
            
            steps_list.append(step)
        return steps_list

    def save_and_deploy(self):
        target_kit = self.get_current_kit()
        if not target_kit:
            QMessageBox.warning(self, "Warning", "No active AAS selected.")
            return
            
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.save_trigger_changes()
            sm_id = f"https://acplt.org/Submodels/ControlLogic_{sanitize_aas_id(target_kit)}"
            encoded_sm_id = encode_id(sm_id)
            sm_url = f"http://localhost:8081/submodels/{encoded_sm_id}"
            
            payload = {
                "idShort": "Rules", "modelType": "Property",
                "value": json.dumps(self.logic_rules), "valueType": "xs:string"
            }
            
            sm_res = requests.get(sm_url, timeout=5)
            if sm_res.status_code == 404:
                submodel_payload = {
                    "id": sm_id, "idShort": "ControlLogic", "modelType": "Submodel",
                    "submodelElements": [payload]
                }
                create_sm_res = requests.post("http://localhost:8081/submodels", json=submodel_payload, headers={"Content-Type": "application/json"}, timeout=5)
                if create_sm_res.status_code not in [200, 201]: raise Exception(f"Failed submodel creation: {create_sm_res.text}")
                    
                item_data = self.combo_aas.currentData()
                shell_id = item_data.get("id")
                link_payload = {"type": "ModelReference", "keys": [{"type": "Submodel", "value": sm_id}]}
                link_res = requests.post(f"http://localhost:8081/shells/{encode_id(shell_id)}/submodel-refs", json=link_payload, headers={"Content-Type": "application/json"}, timeout=5)
                if link_res.status_code not in [200, 201, 204]: raise Exception(f"Failed linking submodel: {link_res.text}")
            else:
                rules_url = f"{sm_url}/submodel-elements/Rules"
                rules_res = requests.get(rules_url, timeout=5)
                if rules_res.status_code == 404:
                    create_prop_res = requests.post(f"{sm_url}/submodel-elements", json=payload, headers={"Content-Type": "application/json"}, timeout=5)
                    if create_prop_res.status_code not in [200, 201]: raise Exception(f"Failed property creation: {create_prop_res.text}")
                else:
                    put_res = requests.put(rules_url, json=payload, headers={"Content-Type": "application/json"}, timeout=5)
                    if put_res.status_code not in [200, 201, 204]: raise Exception(f"Failed property update: {put_res.text}")
                        
            while QApplication.overrideCursor() is not None: QApplication.restoreOverrideCursor()
            QMessageBox.information(self, "Success", f"Rules deployed to '{target_kit}' successfully!")
            self.accept()
        except Exception as e:
            while QApplication.overrideCursor() is not None: QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to deploy rules: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv); window = ConveyorWizard(); window.show(); sys.exit(app.exec())
