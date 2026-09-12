import os
import re
import shutil
import sys
import difflib
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import Tk, StringVar, BooleanVar, END, Text, BOTH, LEFT, RIGHT, X, Y
from tkinter import ttk, filedialog, messagebox

RUN_LINE_PATTERN = re.compile(r"^\s*run\s*=\s*CommandList\\global\\ORFix\\(NNFix|ORFix)\s*$", re.IGNORECASE)
PS_ASSIGN_PATTERN = re.compile(r"^\s*(ps-t\d+)\s*=\s*(.+?)\s*$", re.IGNORECASE)
SECTION_HEADER_PATTERN = re.compile(r"^\s*\[(.+)]\s*$")
SECTION_TARGET_PATTERN = re.compile(r"^\[(CommandList|TextureOverride).+]$", re.IGNORECASE)

ACTION_AUTO = "Auto"
ACTION_ORFIX = "ORFix"
ACTION_NNFIX = "NNFix"
ACTION_SKIP = "Skip"
ACTION_REMOVE = "Remove runs"

ACTION_LABELS = {
    ACTION_AUTO: "自动",
    ACTION_ORFIX: "ORFix",
    ACTION_NNFIX: "NNFix",
    ACTION_SKIP: "跳过",
    ACTION_REMOVE: "移除运行项",
}
ALL_ACTIONS = [ACTION_AUTO, ACTION_ORFIX, ACTION_NNFIX, ACTION_SKIP, ACTION_REMOVE]


@dataclass
class 区段Info:
    header: str
    body: list[str]
    kind: str
    detected_mode: str
    has_ps_assignments: bool
    current_runs: list[str]
    file_path: str
    action: str = ACTION_SKIP
    refs_to_commandlists: list[str] = field(default_factory=list)


def parse_sections(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    sections: list[tuple[str | None, list[str]]] = []
    current_header = None
    current_body: list[str] = []

    preamble: list[str] = []

    for line in lines:
        if SECTION_HEADER_PATTERN.match(line.strip()):
            if current_header is not None:
                sections.append((current_header, current_body))
            elif preamble:
                sections.append((None, preamble))
                preamble = []
            current_header = line.strip()
            current_body = []
        elif current_header is not None:
            current_body.append(line)
        else:
            preamble.append(line)

    if current_header is not None:
        sections.append((current_header, current_body))
    elif preamble:
        sections.append((None, preamble))

    return sections


def detect_mode(body: list[str]) -> str:
    slot_values: dict[str, str] = {}
    for line in body:
        m = PS_ASSIGN_PATTERN.match(line.strip())
        if not m:
            continue
        slot_values[m.group(1).lower()] = m.group(2)

    t0 = slot_values.get("ps-t0", "")
    t1 = slot_values.get("ps-t1", "")
    t2 = slot_values.get("ps-t2", "")

    has_full_triplet = (
        "normalmap" in t0.lower()
        and "diffuse" in t1.lower()
        and "lightmap" in t2.lower()
    )
    has_two_line = "diffuse" in t0.lower() and "lightmap" in t1.lower()

    if has_full_triplet:
        return "Full ps-t0/1/2 (NormalMap, Diffuse, LightMap)"
    if has_two_line:
        return "Two-line ps-t0/1 (Diffuse, LightMap)"
    if slot_values:
        return "Custom ps-t layout"
    return "No ps-t assignments"


def has_ps_assignments(body: list[str]) -> bool:
    for line in body:
        if PS_ASSIGN_PATTERN.match(line.strip()):
            return True
    return False


def suggested_run_for_mode(mode_text: str) -> str | None:
    if mode_text.startswith("Full"):
        return ACTION_ORFIX
    if mode_text.startswith("Two-line"):
        return ACTION_NNFIX
    return None


def list_current_orfix_runs(body: list[str]) -> list[str]:
    runs = []
    for line in body:
        if RUN_LINE_PATTERN.match(line.strip()):
            runs.append(line.strip())
    return runs


def find_commandlist_refs(body: list[str]) -> list[str]:
    refs: list[str] = []
    for line in body:
        stripped = line.strip()
        if not stripped.lower().startswith("run ="):
            continue
        value = stripped.split("=", 1)[1].strip()
        if value.lower().startswith("commandlist") and "\\global\\orfix\\" not in value.lower():
            refs.append(f"[{value}]")
    return sorted(set(refs))


def scan_ini_files(base_dir: str, recursive: bool) -> tuple[list[str], list[区段Info]]:
    ini_files: list[str] = []
    found_sections: list[区段Info] = []

    for root, _, files in os.walk(base_dir, topdown=True):
        if not recursive and os.path.abspath(root) != os.path.abspath(base_dir):
            continue

        for name in files:
            if not name.lower().endswith(".ini"):
                continue

            fpath = os.path.join(root, name)
            ini_files.append(fpath)

            with open(fpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for header, body in parse_sections(lines):
                if header is None:
                    continue
                if not SECTION_TARGET_PATTERN.match(header):
                    continue

                kind = "CommandList" if header.lower().startswith("[commandlist") else "TextureOverride"
                mode = detect_mode(body)
                has_ps = has_ps_assignments(body)
                if not has_ps:
                    continue
                runs = list_current_orfix_runs(body)
                refs = find_commandlist_refs(body)

                found_sections.append(
                    区段Info(
                        header=header,
                        body=body,
                        kind=kind,
                        detected_mode=mode,
                        has_ps_assignments=has_ps,
                        current_runs=runs,
                        file_path=fpath,
                        refs_to_commandlists=refs,
                    )
                )

        if not recursive:
            break

    return ini_files, found_sections


def remove_orfix_runs(body: list[str]) -> tuple[list[str], int]:
    removed = 0
    out = []
    for line in body:
        if RUN_LINE_PATTERN.match(line.strip()):
            removed += 1
            continue
        out.append(line)
    return out, removed


def insert_run_after_last_ps(body: list[str], desired_run: str) -> tuple[list[str], bool]:
    last_ps_idx = None
    for idx, line in enumerate(body):
        if PS_ASSIGN_PATTERN.match(line.strip()):
            last_ps_idx = idx

    if last_ps_idx is None:
        return body, False

    if len(body) > last_ps_idx + 1 and body[last_ps_idx + 1].strip().lower() == desired_run.strip().lower():
        return body, False

    body.insert(last_ps_idx + 1, desired_run + "\n")
    return body, True


def apply_action_to_body(
    body: list[str],
    action: str,
    detected_mode_text: str,
    rename_ps_t69: bool,
    new_ps_slot: str,
    keep_runs_in_place: bool,
    preserve_existing_position: bool,
) -> tuple[list[str], list[str], str | None]:
    changes: list[str] = []

    if action == ACTION_SKIP:
        return body, changes, None

    if rename_ps_t69:
        renamed = 0
        rewritten = []
        for line in body:
            stripped = line.lstrip()
            if stripped.lower().startswith("ps-t69"):
                prefix = line[:len(line) - len(stripped)]
                rewritten.append(prefix + stripped.replace("ps-t69", new_ps_slot, 1))
                renamed += 1
            else:
                rewritten.append(line)
        body = rewritten
        if renamed:
            changes.append(f"Renamed {renamed} ps-t69 line(s) to {new_ps_slot}")

    desired = action
    if action == ACTION_AUTO:
        desired = suggested_run_for_mode(detected_mode_text) or ACTION_SKIP

    if action == ACTION_REMOVE:
        body, removed = remove_orfix_runs(body)
        if removed:
            changes.append(f"Removed {removed} existing ORFix/NNFix run line(s)")
        return body, changes, None

    if desired == ACTION_SKIP:
        return body, changes, None

    desired_line = f"run = CommandList\\global\\ORFix\\{desired}"
    desired_line_l = desired_line.lower()

    existing_orfix_lines = [
        line.strip().lower()
        for line in body
        if RUN_LINE_PATTERN.match(line.strip())
    ]
    has_desired_already = any(line == desired_line_l for line in existing_orfix_lines)

    if preserve_existing_position and has_desired_already:
        changes.append("Kept existing ORFix/NNFix line position (already present)")
        return body, changes, desired

    if keep_runs_in_place:
        updated = []
        existing_run_lines = 0
        replaced_lines = 0
        for line in body:
            stripped = line.lstrip()
            if RUN_LINE_PATTERN.match(stripped):
                existing_run_lines += 1
                prefix = line[:len(line) - len(stripped)]
                new_line = prefix + desired_line + "\n"
                if line != new_line:
                    replaced_lines += 1
                updated.append(new_line)
            else:
                updated.append(line)

        body = updated
        if existing_run_lines:
            if replaced_lines:
                changes.append(f"Updated {replaced_lines} existing ORFix/NNFix run line(s) in place")
            else:
                changes.append("Existing ORFix/NNFix run line(s) already match requested mode")
            return body, changes, desired

        body, inserted = insert_run_after_last_ps(body, desired_line)
        if inserted:
            changes.append(f"Added run line: {desired_line}")
        else:
            changes.append("No run line added (no ps-t anchor found or already present)")
        return body, changes, desired

    body, removed = remove_orfix_runs(body)
    if removed:
        changes.append(f"Removed {removed} existing ORFix/NNFix run line(s)")

    body, inserted = insert_run_after_last_ps(body, desired_line)
    if inserted:
        changes.append(f"Added run line: {desired_line}")
    else:
        changes.append("No run line added (no ps-t anchor found or already present)")

    return body, changes, desired


def rebuild_ini_text(sections: list[tuple[str | None, list[str]]]) -> list[str]:
    out: list[str] = []
    for header, body in sections:
        if header is None:
            out.extend(body)
            continue
        out.append(header + "\n")
        out.extend(body)
    return out


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("ORFix/NNFix 区段管理工具")
        self.root.geometry("1400x850")

        if getattr(sys, "frozen", False):
            default_dir = os.path.dirname(sys.executable)
        else:
            default_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = StringVar(value=default_dir)
        self.recursive = BooleanVar(value=False)
        self.rename_ps_t69_enabled = BooleanVar(value=False)
        self.rename_target_slot = StringVar(value="ps-t1")
        self.keep_runs_in_place = BooleanVar(value=True)
        self.preserve_existing_position = BooleanVar(value=True)
        self.create_backups = BooleanVar(value=True)
        self.theme_mode = StringVar(value="Dark")

        self.ini_files: list[str] = []
        self.sections: list[区段Info] = []
        self.section_map: dict[str, 区段Info] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=X)

        ttk.Label(top, text="文件夹：").pack(side=LEFT)
        ttk.Entry(top, textvariable=self.base_dir, width=70).pack(side=LEFT, padx=6)
        ttk.Button(top, text="浏览", command=self.pick_folder).pack(side=LEFT)
        ttk.Checkbutton(top, text="扫描子文件夹", variable=self.recursive).pack(side=LEFT, padx=10)
        ttk.Button(top, text="扫描 .ini 文件", command=self.scan).pack(side=LEFT, padx=10)
        ttk.Label(top, text="主题：").pack(side=LEFT, padx=(14, 4))
        theme_combo = ttk.Combobox(top, textvariable=self.theme_mode, values=["Dark", "Light"], width=8, state="readonly")
        theme_combo.pack(side=LEFT)
        theme_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_theme())

        tools = ttk.Frame(self.root, padding=8)
        tools.pack(fill=X)

        ttk.Label(tools, text="为选中项设置操作：").pack(side=LEFT)
        for action in ALL_ACTIONS:
            ttk.Button(tools, text=ACTION_LABELS.get(action, action), command=lambda a=action: self.set_selected_action(a)).pack(side=LEFT, padx=3)

        ttk.Button(tools, text="将全部设为跳过", command=self.set_all_skip).pack(side=LEFT, padx=10)
        ttk.Button(tools, text="将全部设为自动", command=self.set_all_auto).pack(side=LEFT, padx=5)

        rename_frame = ttk.Frame(self.root, padding=8)
        rename_frame.pack(fill=X)

        ttk.Checkbutton(
            rename_frame,
            text="可选的特殊工具：重命名 ps-t69 行",
            variable=self.rename_ps_t69_enabled,
        ).pack(side=LEFT)

        ttk.Label(rename_frame, text="to").pack(side=LEFT, padx=(10, 3))
        slot_values = [f"ps-t{i}" for i in range(0, 80)]
        ttk.Combobox(rename_frame, textvariable=self.rename_target_slot, values=slot_values, width=8, state="readonly").pack(side=LEFT)

        ttk.Label(
            rename_frame,
            text="（仅在极少数情况下使用）",
            foreground="#aa5500",
        ).pack(side=LEFT, padx=10)

        center = ttk.Frame(self.root, padding=8)
        center.pack(fill=BOTH, expand=True)

        self.tree = ttk.Treeview(
            center,
            columns=("action", "section", "kind", "detected", "current", "file"),
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("action", text="操作")
        self.tree.heading("section", text="区段")
        self.tree.heading("kind", text="类型")
        self.tree.heading("detected", text="检测到的 ps-t 模式")
        self.tree.heading("current", text="当前 ORFix/NNFix 运行项")
        self.tree.heading("file", text="文件")

        self.tree.column("action", width=100)
        self.tree.column("section", width=320)
        self.tree.column("kind", width=110)
        self.tree.column("detected", width=320)
        self.tree.column("current", width=260)
        self.tree.column("file", width=360)

        yscroll = ttk.Scrollbar(center, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        yscroll.pack(side=RIGHT, fill=Y)

        opts = ttk.Frame(self.root, padding=8)
        opts.pack(fill=X)
        self.copy_to_commandlists = BooleanVar(value=False)
        ttk.Checkbutton(
            opts,
            text="如果 TextureOverride 添加 ORFix/NNFix，也在其引用的 [CommandList...] 区段中添加相同运行项",
            variable=self.copy_to_commandlists,
        ).pack(side=LEFT)

        opts2 = ttk.Frame(self.root, padding=8)
        opts2.pack(fill=X)
        ttk.Checkbutton(
            opts2,
            text="复杂模组安全模式：保持 ORFix/NNFix 运行行原位（不移动）",
            variable=self.keep_runs_in_place,
        ).pack(side=LEFT)
        ttk.Checkbutton(
            opts2,
            text="保持现有正确的 ORFix/NNFix 行位置（不重新排序）",
            variable=self.preserve_existing_position,
        ).pack(side=LEFT, padx=20)
        ttk.Checkbutton(
            opts2,
            text="应用时创建备份文件",
            variable=self.create_backups,
        ).pack(side=LEFT, padx=20)

        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill=BOTH, expand=False)
        ttk.Button(bottom, text="预览更改", command=self.preview).pack(side=LEFT)
        ttk.Button(bottom, text="应用更改", command=self.apply).pack(side=LEFT, padx=10)

        split = ttk.Panedwindow(self.root, orient="horizontal")
        split.pack(fill=BOTH, expand=True, padx=8, pady=8)

        left_panel = ttk.Frame(split)
        right_panel = ttk.Frame(split)
        split.add(left_panel, weight=1)
        split.add(right_panel, weight=3)

        ttk.Label(left_panel, text="活动日志", padding=(0, 0)).pack(fill=X)
        self.log = Text(left_panel, height=20)
        self.log.pack(fill=BOTH, expand=True)

        ttk.Label(right_panel, text="预览（ini 差异）", padding=(0, 0)).pack(fill=X)
        self.preview_text = Text(right_panel, height=20)
        self.preview_text.pack(fill=BOTH, expand=True)

        self.apply_theme()

    def apply_theme(self) -> None:
        mode = self.theme_mode.get().strip().lower()
        dark = mode != "light"

        if dark:
            colors = {
                "bg": "#1f1f1f",
                "fg": "#e6e6e6",
                "panel": "#2b2b2b",
                "accent": "#3a3a3a",
                "select": "#264f78",
                "select_fg": "#ffffff",
            }
        else:
            colors = {
                "bg": "#f0f0f0",
                "fg": "#111111",
                "panel": "#ffffff",
                "accent": "#d9d9d9",
                "select": "#cce8ff",
                "select_fg": "#000000",
            }

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.root.configure(background=colors["bg"])
        style.configure(".", background=colors["bg"], foreground=colors["fg"], fieldbackground=colors["panel"])
        style.configure("TFrame", background=colors["bg"])
        style.configure("TLabel", background=colors["bg"], foreground=colors["fg"])
        style.configure("TCheckbutton", background=colors["bg"], foreground=colors["fg"])
        style.configure("TButton", background=colors["accent"], foreground=colors["fg"])
        style.configure("TEntry", fieldbackground=colors["panel"], foreground=colors["fg"])
        style.configure("TCombobox", fieldbackground=colors["panel"], foreground=colors["fg"])
        style.configure(
            "Treeview",
            background=colors["panel"],
            fieldbackground=colors["panel"],
            foreground=colors["fg"],
        )
        style.configure("Treeview.Heading", background=colors["accent"], foreground=colors["fg"])
        style.map("Treeview", background=[("selected", colors["select"])], foreground=[("selected", colors["select_fg"])])

        self.log.configure(
            background=colors["panel"],
            foreground=colors["fg"],
            insertbackground=colors["fg"],
            selectbackground=colors["select"],
            selectforeground=colors["select_fg"],
        )
        self.preview_text.configure(
            background=colors["panel"],
            foreground=colors["fg"],
            insertbackground=colors["fg"],
            selectbackground=colors["select"],
            selectforeground=colors["select_fg"],
        )

    def pick_folder(self) -> None:
        initial = self.base_dir.get().strip()
        if not initial or not os.path.isdir(initial):
            initial = os.path.expanduser("~")

        try:
            self.root.update_idletasks()
            picked = filedialog.askdirectory(
                parent=self.root,
                initialdir=initial,
                mustexist=True,
                title="选择包含 ini 文件的文件夹",
            )
        except Exception as exc:
            messagebox.showerror("浏览 failed", f"Could not open folder picker:\n{exc}")
            return

        if picked:
            self.base_dir.set(picked)

    def log_line(self, text: str) -> None:
        self.log.insert(END, text + "\n")
        self.log.see(END)

    def scan(self) -> None:
        base_dir = self.base_dir.get().strip()
        if not base_dir or not os.path.isdir(base_dir):
            messagebox.showerror("文件夹无效", "请先选择有效的文件夹。")
            return

        self.ini_files, self.sections = scan_ini_files(base_dir, self.recursive.get())
        self.section_map.clear()

        self.tree.delete(*self.tree.get_children())

        for idx, section in enumerate(self.sections):
            key = f"{section.file_path}||{section.header}||{idx}"
            self.section_map[key] = section
            current = ", ".join(section.current_runs) if section.current_runs else "None"
            self.tree.insert(
                "",
                END,
                iid=key,
                values=(
                    section.action,
                    section.header,
                    section.kind,
                    section.detected_mode,
                    current,
                    section.file_path,
                ),
            )

        self.log_line(f"Scanned {len(self.ini_files)} ini file(s), loaded {len(self.sections)} target section(s).")

    def set_selected_action(self, action: str) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("未选择", "请先选择一个或多个区段。")
            return

        for iid in selected:
            sec = self.section_map[iid]
            sec.action = action
            row = list(self.tree.item(iid, "values"))
            row[0] = action
            self.tree.item(iid, values=row)

        self.log_line(f"Set action '{action}' for {len(selected)} section(s).")

    def set_all_auto(self) -> None:
        for iid, sec in self.section_map.items():
            sec.action = ACTION_AUTO
            row = list(self.tree.item(iid, "values"))
            row[0] = ACTION_AUTO
            self.tree.item(iid, values=row)
        self.log_line("Set all sections to Auto.")

    def set_all_skip(self) -> None:
        for iid, sec in self.section_map.items():
            sec.action = ACTION_SKIP
            row = list(self.tree.item(iid, "values"))
            row[0] = ACTION_SKIP
            self.tree.item(iid, values=row)
        self.log_line("Set all sections to Skip.")

    def _build_preview_or_output(self, write_files: bool) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        change_log_per_file: dict[str, list[str]] = {}
        output_lines_per_file: dict[str, list[str]] = {}

        sections_by_file: dict[str, list[区段Info]] = {}
        for sec in self.sections:
            sections_by_file.setdefault(sec.file_path, []).append(sec)

        for fpath in self.ini_files:
            with open(fpath, "r", encoding="utf-8") as f:
                original_lines = f.readlines()
            parsed = parse_sections(original_lines)

            header_to_index: dict[str, int] = {}
            for idx, (header, _) in enumerate(parsed):
                if header is not None:
                    header_to_index[header.lower()] = idx

            local_changes: list[str] = []
            file_sections = sections_by_file.get(fpath, [])

            for sec in file_sections:
                idx = header_to_index.get(sec.header.lower())
                if idx is None:
                    continue

                header, body = parsed[idx]
                body_copy = body[:]
                updated_body, changes, decided = apply_action_to_body(
                    body_copy,
                    sec.action,
                    sec.detected_mode,
                    self.rename_ps_t69_enabled.get(),
                    self.rename_target_slot.get(),
                    self.keep_runs_in_place.get(),
                    self.preserve_existing_position.get(),
                )

                if changes:
                    for c in changes:
                        local_changes.append(f"{header}: {c}")
                    parsed[idx] = (header, updated_body)

                if (
                    self.copy_to_commandlists.get()
                    and sec.kind == "TextureOverride"
                    and decided in (ACTION_ORFIX, ACTION_NNFIX)
                ):
                    desired_line = f"run = CommandList\\global\\ORFix\\{decided}"
                    for cmd_header in sec.refs_to_commandlists:
                        cmd_idx = header_to_index.get(cmd_header.lower())
                        if cmd_idx is None:
                            continue
                        cmd_h, cmd_body = parsed[cmd_idx]
                        cmd_updated, cmd_changes, _ = apply_action_to_body(
                            cmd_body[:],
                            decided,
                            detect_mode(cmd_body),
                            False,
                            self.rename_target_slot.get(),
                            self.keep_runs_in_place.get(),
                            self.preserve_existing_position.get(),
                        )
                        parsed[cmd_idx] = (cmd_h, cmd_updated)
                        if cmd_changes:
                            local_changes.append(f"{cmd_h}: Mirrored from {header} -> {desired_line}")

            if local_changes:
                rebuilt = rebuild_ini_text(parsed)
                output_lines_per_file[fpath] = rebuilt
                change_log_per_file[fpath] = local_changes

                if write_files:
                    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    backup_name = f"{os.path.basename(fpath)}_OrfixNNfix_{timestamp}.bak"
                    backup_path = os.path.join(os.path.dirname(fpath), backup_name)
                    if self.create_backups.get():
                        shutil.copyfile(fpath, backup_path)
                    with open(fpath, "w", encoding="utf-8") as out:
                        out.writelines(rebuilt)
                    if self.create_backups.get():
                        local_changes.insert(0, f"Backup: {backup_path}")
                    else:
                        local_changes.insert(0, "备份：已禁用")

        return change_log_per_file, output_lines_per_file

    def preview(self) -> None:
        self.log_line("--- 预览 ---")
        self.preview_text.delete("1.0", END)
        change_log, output_lines_per_file = self._build_preview_or_output(write_files=False)
        if not change_log:
            self.log_line("根据当前操作设置，未检测到更改。")
            self.preview_text.insert(END, "根据当前操作设置，未检测到更改。\n")
            return

        for fpath, changes in change_log.items():
            self.log_line(f"文件: {fpath}")
            for c in changes:
                self.log_line(f"  - {c}")

            with open(fpath, "r", encoding="utf-8") as f:
                original_lines = f.readlines()
            updated_lines = output_lines_per_file.get(fpath, original_lines)

            diff_lines = list(
                difflib.unified_diff(
                    original_lines,
                    updated_lines,
                    fromfile=f"{fpath} (current)",
                    tofile=f"{fpath} (preview)",
                    lineterm="",
                    n=3,
                )
            )

            if diff_lines:
                self.preview_text.insert(END, f"文件: {fpath}\n")
                for line in diff_lines:
                    self.preview_text.insert(END, line + "\n")
                self.preview_text.insert(END, "\n")
            else:
                self.preview_text.insert(END, f"文件: {fpath}\n(No textual diff)\n\n")

    def apply(self) -> None:
        if not self.ini_files:
            messagebox.showinfo("尚未加载内容", "请先扫描 ini 文件。")
            return

        if not messagebox.askyesno("应用更改", "应用更改 and create timestamp backups?"):
            return

        self.log_line("--- 应用 ---")
        change_log, _ = self._build_preview_or_output(write_files=True)
        if not change_log:
            self.log_line("未应用更改（没有需要更改的内容）。")
            return

        changed_files = 0
        for fpath, changes in change_log.items():
            changed_files += 1
            self.log_line(f"Updated: {fpath}")
            for c in changes:
                self.log_line(f"  - {c}")

        self.log_line(f"完成. Updated {changed_files} file(s).")
        messagebox.showinfo("完成", f"Updated {changed_files} file(s).")


def main() -> None:
    root = Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    app = App(root)
    app.log_line("GUI 已就绪。扫描文件夹，选择区段操作，预览后应用。")
    root.mainloop()


if __name__ == "__main__":
    main()
