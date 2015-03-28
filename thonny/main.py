#! /usr/bin/python3
# -*- coding: utf-8 -*-

"""
Thonny, Python IDE for beginners Copyright (C) 2014 Aivar Annamaa

This program is free software: you can redistribute it and/or modify it under the 
terms of the GNU General Public License as published by the Free Software Foundation, 
either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; 
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. 
See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program. 
If not, see <http://www.gnu.org/licenses/>.
"""

import sys
import traceback
import os.path
from distutils.version import StrictVersion
import logging
import time
import gettext
import re
import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as tkMessageBox

from thonny import ui_utils
from thonny import stack
from thonny import vm_proxy
from thonny.config import prefs
from thonny.about import AboutDialog
from thonny.static import AstFrame
from thonny.code import EditorNotebook
from thonny.shell import ShellFrame
from thonny.memory import GlobalsFrame, HeapFrame, ObjectInspector
from thonny.browser import FileBrowser
from thonny.common import DebuggerCommand, ToplevelCommand, DebuggerResponse,\
    InlineCommand, quote_path_for_shell
from thonny.ui_utils import Command, notebook_contains
from thonny import user_logging
from thonny import misc_utils
import thonny.refactor
import subprocess
import thonny.outline




THONNY_USER_DIR = os.path.expanduser(os.path.join("~", ".thonny"))





class Thonny(tk.Tk):
    def __init__(self, src_dir):
        self.last_manual_debugger_command_sent = None # TODO: hack
        self.step_over = False
        
        self.src_dir = src_dir
        tk.Tk.__init__(self)
        tk.Tk.report_callback_exception = self.on_tk_exception
        
        self.logger = logging.getLogger("thonny.main")
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(logging.StreamHandler(sys.stdout))
        
        self.user_logger = user_logging.UserEventLogger(self.new_user_log_file())
        user_logging.USER_LOGGER = self.user_logger # TODO: ugly
        
        gettext.translation('thonny',
                    os.path.join(src_dir, "locale"), 
                    languages=[prefs["general.language"], "en"]).install()
        
        self.createcommand("::tk::mac::OpenDocument", self._mac_open_document)
        self.createcommand("::tk::mac::OpenApplication", self._mac_open_application)
        self.createcommand("::tk::mac::ReopenApplication", self._mac_reopen_application)
        self.set_icon()
        
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        #showinfo("sys.argv", str(sys.argv))
        
        self._vm = vm_proxy.VMProxy(prefs["cwd"], src_dir)
        self._update_title()
        
        # UI items, positions, sizes
        geometry = "{0}x{1}+{2}+{3}".format(prefs["layout.width"], prefs["layout.height"],
                                               prefs["layout.left"], prefs["layout.top"])
        if prefs["layout.zoomed"]:
            ui_utils.set_zoomed(self, True)
        self.geometry(geometry)
        
        ui_utils.setup_style()
        self._init_widgets()
        
        self._init_commands()

        
        
        # events ---------------------------------------------
        
        # There are 3 kinds of events:
        #    - commands from user (menu and toolbar events are bound in respective methods)
        #    - notifications about asynchronous debugger responses 
        #    - notifications about new output from the running program
        
        ui_utils.start_keeping_track_of_held_keys(self)
        
        # KeyRelease may also trigger a debugger command
        # self.bind_all("<KeyRelease>", self._check_issue_goto_before_or_after, "+") # TODO: 
        
        # start listening to backend process
        self._poll_vm_messages()
        self._advance_background_tk_mainloop()
        self.bind("<FocusIn>", self.on_get_focus, "+")
        self.bind("<FocusOut>", self.on_lose_focus, "+")
        # self.bind('<Expose>', self._expose, "+")
        # self.focus_force()
        self.editor_book.load_startup_files()
        self.editor_book.focus_current_editor()
    
    
    def _init_widgets(self):
        
        self.main_frame= ttk.Frame(self) # just a backgroud behind padding of main_pw, without this OS X leaves white border 
        self.main_frame.grid(sticky=tk.NSEW)
        self.toolbar = ttk.Frame(self.main_frame, padding=0) # TODO: height=30 ?
        
        self.main_pw   = ui_utils.create_PanedWindow(self.main_frame, orient=tk.HORIZONTAL)
        self.right_pw  = ui_utils.create_PanedWindow(self.main_pw, orient=tk.VERTICAL)
        self.center_pw = ui_utils.create_PanedWindow(self.main_pw, orient=tk.VERTICAL)
        
        self.toolbar.grid(column=0, row=0, sticky=tk.NSEW, padx=10)
        self._init_populate_toolbar()
        self.main_pw.grid(column=0, row=1, sticky=tk.NSEW, padx=10, pady=10)
        
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        
        self.main_frame.columnconfigure(0, weight=1)
        self.main_frame.rowconfigure(1, weight=1)
        
        self.browse_book = ui_utils.PanelBook(self.main_pw)
        self.editor_book = EditorNotebook(self.center_pw)
        self.main_pw.add(self.center_pw, minsize=150, width=prefs["layout.center_width"])
        self.file_browser = FileBrowser(self, self.editor_book)
        self.browse_book.add(self.file_browser, text="Files")
        self.cmd_update_browser_visibility(False)
        
        self.cmd_update_memory_visibility(False)
        
        self.center_pw.add(self.editor_book, minsize=150)
        
        self.control_book = ui_utils.PanelBook(self.center_pw)
        self.center_pw.add(self.control_book, minsize=50)
        self.shell = ShellFrame(self.control_book, self._vm, self.editor_book)
        self.stack = stack.StackPanel(self.control_book, self._vm, self.editor_book)
        self.ast_frame = AstFrame(self.control_book)
        
        self.control_book.add(self.shell, text=_("Shell")) # TODO: , underline=0
        #self.control_book.add(self.stack, text="Stack") # TODO: , underline=1
        
         
        self.globals_book = ui_utils.PanelBook(self.right_pw)
        self.globals_frame = GlobalsFrame(self.globals_book)
        self.globals_book.add(self.globals_frame, text=_("Variables"))
        self.right_pw.add(self.globals_book, minsize=50)

                 
        self.outline_book = ui_utils.PanelBook(self.right_pw)
        self.outline_frame = thonny.outline.OutlineFrame(self.outline_book, self.editor_book)
        self.outline_book.add(self.outline_frame, text=_("Outline"))
        
        
        self.heap_book = ui_utils.PanelBook(self.right_pw)
        self.heap_frame = HeapFrame(self.heap_book)
        self.heap_book.add(self.heap_frame, text=_("Heap")) 
        if prefs["values_in_heap"]:
            self.right_pw.add(self.heap_book, minsize=50)
        
        self.info_book = ui_utils.PanelBook(self.right_pw)
        self.inspector_frame = ObjectInspector(self.info_book)
        self.info_book.add(self.inspector_frame, text="Object info")
        #self.right_pw.add(self.info_book, minsize=50)
        self.cmd_update_inspector_visibility()

    
    def _init_commands(self):
        
        # TODO: see idlelib.macosxSupports
        self.createcommand("tkAboutDialog", self.cmd_about)
        # https://www.tcl.tk/man/tcl8.6/TkCmd/tk_mac.htm
        self.createcommand("::tk::mac::ShowPreferences", lambda: print("Prefs"))

        
        # declare menu structure
        self._menus = [
            ('file', 'File', [
                Command('new_file',     _('New'),         'Ctrl+N',       self.editor_book),
                Command('open_file',    _('Open...'),     'Ctrl+O',       self.editor_book),
                Command('close_file',   _('Close'),       'Ctrl+W',       self.editor_book),
                "---",
                Command('save_file',    'Save',        'Ctrl+S',       self.editor_book.get_current_editor),
                Command('save_file_as', 'Save as...',  'Shift+Ctrl+S', self.editor_book.get_current_editor),
#                 "---",
#                 Command('print',    'Print (via browser)',        'Ctrl+P',       self.editor_book.get_current_editor),
#                 "---",
#                 Command('TODO:', '1 prax11/kollane.py', None, None),
#                 Command('TODO:', '2 kodutööd/vol1_algne.py', None, None),
#                 Command('TODO:', '3 stat.py', None, None),
#                 Command('TODO:', '4 praxss11/kollane.py', None, None),
#                 Command('TODO:', '5 koduqwertööd/vol1_algne.py', None, None),
#                 Command('TODO:', '6 stasst.py', None, None),
#                 "---",
                Command('exit', 'Exit', None, self),
            ]),
            ('edit', 'Edit', [
                Command('undo',         'Undo',         'Ctrl+Z', self._find_current_edit_widget, system_bound=True), 
                Command('redo',         'Redo',         'Ctrl+Y', self._find_current_edit_widget, system_bound=True),
                "---", 
                Command('cut',          'Cut',          'Ctrl+X', self._find_current_edit_widget, system_bound=True), 
                Command('copy',         'Copy',         'Ctrl+C', self._find_current_edit_widget, system_bound=True), 
                Command('paste',        'Paste',        'Ctrl+V', self._find_current_edit_widget, system_bound=True),
                "---", 
                Command('select_all',   'Select all',   'Ctrl+A', self._find_current_edit_widget),
#                 "---",        
#                 Command('find_next',    'Find next',    'F3',     self._find_current_edit_widget),
                 
            ]),
            ('view', 'View', [
                Command('update_browser_visibility', 'Show browser',  None, self,
                        kind="checkbutton", variable_name="layout.browser_visible"),
                Command('update_memory_visibility', 'Show variables',  None, self,
                        kind="checkbutton", variable_name="layout.memory_visible"),
                Command('update_inspector_visibility', 'Show object inspector',  None, self,
                        kind="checkbutton", variable_name="layout.inspector_visible"),
                "---",
                Command('increase_font_size', 'Increase font size', 'Ctrl++', self),
                Command('decrease_font_size', 'Decrease font size', 'Ctrl+-', self),
                "---",
                Command('update_memory_model', 'Show values in heap',  None, self,
                        kind="checkbutton", variable_name="values_in_heap"),
                #Command('update_debugging_mode', 'Enable advanced debugging',  None, self,
                #        kind="checkbutton", variable_name="advanced_debugging"),
                "---",
                Command('focus_editor', "Focus editor", "F11", self),
                Command('focus_shell', "Focus shell", "F12", self),
                Command('show_ast', "Show AST", None, self),
                Command('show_replayer', "Show Replayer", None, self),
                Command('preferences', 'Preferences', None, self) 
            ]),
#             ('code', 'Code', [
#                 Command('TODO:', 'Indent selection',         "Tab", self.editor_book.get_current_editor), 
#                 Command('TODO:', 'Dedent selection',         "Shift+Tab", self.editor_book.get_current_editor),
#                 "---", 
#                 Command('TODO:', 'Comment out selection',    "Ctrl+3", self.editor_book.get_current_editor), 
#                 Command('TODO:', 'Uncomment selection',      "Shift+Ctrl+3", self.editor_book.get_current_editor), 
#             ]),
            ('run', 'Run', [
                Command('run_current_script',       'Run current script',        'F5',  self), 
                Command('debug_current_script',     'Debug current script',  'Ctrl+F5', self),
#                 Command('run_current_file',         'Run current file',        None,  self), 
#                 Command('debug_current_file',       'Debug current file',  None, self), 
#                 "---", 
#                 Command('run_current_cell',         'Run current cell',        None,  self), 
#                 Command('debug_current_cell',       'Debug current cell',  None, self), 
#                 "---", 
#                 Command('run_current_selection',    'Run current selection',        None,  self), 
#                 Command('debug_current_selection',  'Debug current selection',  None, self), 
#                 "---", 
#                 Command('run_main_script',          'Run main script',        'Shift+F5',  self), 
#                 Command('debug_main_script',        'Debug main script',  'Ctrl+Shift+F5', self), 
#                 Command('run_main_file',            'Run main file',        None,  self), 
#                 Command('debug_main_file',          'Debug main file',  None, self), 
#                 "---", 
                Command('reset',                 'Stop/Reset',       None, self),
                "---", 
                Command('exec',                 'Step over', "F7", self),
#                Command('zoom',                 'Zoom in',               "F8", self),
                Command('step',                 'Step',                  "F9", self),
                "---", 
                Command('set_auto_cd', 'Auto-cd to script dir',  None, self,
                        kind="checkbutton", variable_name="run.auto_cd"),
            ]),
            ('misc', 'Misc', [
                Command('open_user_dir',   'Open Thonny user folder',        None, self), 
            ]),
            ('help', 'Help', [
                Command('help',             'Thonny help',        None, self), 
            ]),
        ]

        #TODO - implement a better way to conditionally add the below items
        if prefs["experimental.find_feature_enabled"]:
            self._menus[1][2].append("---");
            self._menus[1][2].append(Command('find',         'Find',         'Ctrl+F', self._find_current_edit_widget));

        if prefs["experimental.autocomplete_feature_enabled"]:
            self._menus[1][2].append("---");
            self._menus[1][2].append(Command('autocomplete',         'Autocomplete',         'Ctrl+space', self._find_current_edit_widget));            

        if prefs["experimental.outline_feature_enabled"]:
            self._menus[2][2].append("---");
            self._menus[2][2].append(Command('update_outline_visibility',         'Outline',         'Alt+O', self));

        if prefs["experimental.refactor_rename_feature_enabled"]:
            self._menus[1][2].append("---");
            self._menus[1][2].append(Command('refactor_rename',         'Rename identifier',         None, self)); 

        if prefs["experimental.comment_toggle_enabled"]:
            self._menus[1][2].append("---");
            self._menus[1][2].append(Command('comment_in',         'Comment in',         'Ctrl+3', self._find_current_edit_widget)); 
            self._menus[1][2].append(Command('comment_out',         'Comment out',         'Ctrl+4', self._find_current_edit_widget));
                
        # TODO:
        """
        if misc_utils.running_on_mac_os():
            self.menus[-2][2].append (
                Command('mac_add_download_assessment',   'Allow opening py files from browser ...',        None, self)
            )
        """
        
        
        ## make plaftform specific tweaks
        if misc_utils.running_on_mac_os():
            # insert app menu with "about" and "preferences"
            self._menus.insert(0, ('apple', 'Thonny', [
                Command('about', 'About Thonny', None, self),
                # TODO: tkdocs says preferences are added automatically.
                # How can I connect with it?
            ]))
            
            # use Command instead of Ctrl in accelerators
            for __, __, items in self._menus:
                for item in items:
                    if isinstance(item, Command) and isinstance(item.accelerator, str):
                        item.accelerator = item.accelerator.replace("Ctrl", "Command") 
        else:
            # insert "about" to Help (last) menu ...
            self._menus[-1][2].append(Command('about', 'About Thonny', None, self))
            

        # create actual widgets and bind the shortcuts
        self.option_add('*tearOff', tk.FALSE)
        menubar = tk.Menu(self)
        self['menu'] = menubar
        
        for name, label, items in self._menus:
            menu = tk.Menu(menubar, name=name)
            menubar.add_cascade(menu=menu, label=label)
            menu["postcommand"] = lambda name=name, menu=menu: self._update_menu(name, menu)
            
            for item in items:
                if item == "---":
                    menu.add_separator()
                elif isinstance(item, Command):
                    menu.add(item.kind,
                        label=item.label,
                        accelerator=item.accelerator,
                        value=item.value,
                        variable=item.variable,
                        command=lambda cmd=item: cmd.execute())
                    
                    if (item.accelerator is not None and not item.system_bound):
                        # create event sequence out of accelerator 
                        # tk wants Control, not Ctrl
                        sequence = item.accelerator.replace("Ctrl", "Control")
                        
                        sequence = sequence.replace("+-", "+minus")
                        sequence = sequence.replace("++", "+plus")
                        
                        # it's customary to show keys with capital letters
                        # but tk would treat this as pressing with shift
                        parts = sequence.split("+")
                        if len(parts[-1]) == 1:
                            parts[-1] = parts[-1].lower()
                        
                        # tk wants "-" between the parts 
                        sequence = "-".join(parts)
                        
                        # bind the event
                        try:
                            self.bind_all("<"+sequence+">", lambda e, cmd=item: cmd.execute(e), "+")
                        except:
                            # TODO: Command+C for Mac
                            # TODO: refactor
                            tk.messagebox.showerror("Internal error. Use Ctrl+C to copy",
                                traceback.format_exc())
                            
                        
                        # TODO: temporary extra key for step
                        if item.cmd_id == "step":
                            self.bind_all("<Control-t>", lambda e, cmd=item: cmd.execute(e), "+")


        
        
        
        #variables_var = tk.BooleanVar()
        #variables_var.set(True)
        #var_menu = view_menu.add_checkbutton(label="Variables", value=1, variable=variables_var, command=showViews)
        #def showViews():
        #    if variables_var.get():
        #        memory_pw.remove(globals_frame)
        #    else:
        #        memory_pw.pane

    def _init_populate_toolbar(self): 
        def on_kala_button():
            self.editor_book.demo_editor.set_read_only(not self.editor_book.demo_editor.read_only)
        
        top_spacer = ttk.Frame(self.toolbar, height=5)
        top_spacer.grid(row=0, column=0, columnspan=100)
        
        self.images = {}
        self.toolbar_buttons = {}
        col = 1
        
        for name in ('file.new_file', 'file.open_file', 'file.save_file', 
                     '-', 'run.run_current_script',
                          'run.debug_current_script',
                          'run.step',
                          'run.reset'):
            
            if name == '-':
                hor_spacer = ttk.Frame(self.toolbar, width=15)
                hor_spacer.grid(row=0, column=col)
            else:
                img = tk.PhotoImage(file=misc_utils.get_res_path(name + ".gif"))
            
                btn = ttk.Button(self.toolbar, 
                                 command=on_kala_button, 
                                 image=img, 
                                 text="?",
                                 style="Toolbutton",
                                 state=tk.DISABLED)
                btn.grid(row=1, column=col, padx=0, pady=0)
            
                self.images[name] = img
                self.toolbar_buttons[name] = btn
                
            col += 1 
        
    def _advance_background_tk_mainloop(self):
        if self._vm.get_state() == "toplevel":
            self._vm.send_command(InlineCommand(command="tkupdate"))
        self.after(50, self._advance_background_tk_mainloop)
        
    def _poll_vm_messages(self):
        # I chose polling instead of event_generate
        # because event_generate across threads is not reliable
        while True:
            msg = self._vm.fetch_next_message()
            if not msg:
                break
            
            # skip some events
            if (isinstance(msg, DebuggerResponse) 
                and hasattr(msg, "tags") 
                and "call_function" in msg.tags
                and not prefs["debugging.expand_call_functions"]):
                
                self._check_issue_debugger_command(DebuggerCommand(command="step"), automatic=True)
                continue
                
            if hasattr(msg, "success") and not msg.success:
                print("_poll_vm_messages, not success")
                self.bell()
            
            self.shell.handle_vm_message(msg)
            self.stack.handle_vm_message(msg)
            self.editor_book.handle_vm_message(msg)
            self.globals_frame.handle_vm_message(msg)
            self.heap_frame.handle_vm_message(msg)
            self.inspector_frame.handle_vm_message(msg)
            
            prefs["cwd"] = self._vm.cwd
            self._update_title()
            
            # automatically advance from some events
            if (isinstance(msg, DebuggerResponse) 
                and msg.state in ("after_statement", "after_suite", "before_suite")
                and not prefs["debugging.detailed_steps"]
                or self.continue_with_step_over(self.last_manual_debugger_command_sent, msg)):
                
                self._check_issue_debugger_command(DebuggerCommand(command="step"), automatic=True)
            
            self.update_idletasks()
            
        self.after(50, self._poll_vm_messages)
    
    def continue_with_step_over(self, cmd, msg):
        if not self.step_over:
            print("Not step_over")
            return False
        
        if not isinstance(msg, DebuggerResponse):
            return False
        
        if cmd is None:
            return False
        
        if msg.state not in ("before_statement", "before_expression", "after_expression"):
            # TODO: hack, may want after_statement
            return True
        
        if msg.frame_id != cmd.frame_id:
            return True
        
        if msg.focus.is_smaller_in(cmd.focus):
            print("smaller")
            return True
        else:
            print("outside")
            return False
        
    
    def cmd_about(self):
        AboutDialog(self, self._get_version())
    
    def cmd_run_current_script_enabled(self):
        return (self._vm.get_state() == "toplevel"
                and self.editor_book.get_current_editor() is not None)
    
    def cmd_run_current_script(self):
        self._execute_current("Run")
    
    def cmd_debug_current_script_enabled(self):
        return self.cmd_run_current_script_enabled()
    
    def cmd_debug_current_script(self):
        self._execute_current("Debug")
        
    def cmd_run_current_file_enabled(self):
        return self.cmd_run_current_script_enabled()
    
    def cmd_run_current_file(self):
        self._execute_current("run")
    
    def cmd_debug_current_file_enabled(self):
        return self.cmd_run_current_script_enabled()
    
    def cmd_debug_current_file(self):
        self._execute_current("debug")
    
    def cmd_increase_font_size(self):
        self._change_font_size(1)
    
    def cmd_decrease_font_size(self):
        self._change_font_size(-1)
    
    def _change_font_size(self, delta):
        self.shell.change_font_size(delta)
        self.editor_book.change_font_size(delta)
        self.globals_frame.change_font_size(delta)
        # TODO:
        #self.builtins_frame.change_font_size(delta)
        self.heap_frame.change_font_size(delta)
    
    def _execute_current(self, cmd_name, text_range=None):
        """
        This method's job is to create a command for running/debugging
        current file/script and submit it to shell
        """
        
        editor = self.editor_book.get_current_editor()
        if not editor:
            return

        filename = editor.get_filename(True)
        if not filename:
            return
        
        # changing dir may be required
        script_dir = os.path.dirname(filename)
        
        if (prefs["run.auto_cd"] and cmd_name[0].isupper()
            and self._vm.cwd != script_dir):
            # create compound command
            # start with %cd
            cmd_line = "%cd " + quote_path_for_shell(script_dir) + "\n"
            next_cwd = script_dir
        else:
            # create simple command
            cmd_line = ""
            next_cwd = self._vm.cwd
        
        # append main command (Run, run, Debug or debug)
        rel_filename = os.path.relpath(filename, next_cwd)
        cmd_line += "%" + cmd_name + " " + quote_path_for_shell(rel_filename) + "\n"
        if text_range is not None:
            "TODO: append range indicators" 
        
        # submit to shell (shell will execute it)
        self.shell.submit_magic_command(cmd_line)
        self.step_over = False
    
    
    def cmd_reset(self):
        self._vm.send_command(ToplevelCommand(command="Reset", globals_required="__main__"))
    
    def cmd_update_browser_visibility(self, adjust_window_width=True):
        if prefs["layout.browser_visible"] and not self.browse_book.winfo_ismapped():
            if adjust_window_width:
                self._check_update_window_width(+prefs["layout.browser_width"]+ui_utils.SASHTHICKNESS)
            self.main_pw.add(self.browse_book, minsize=150, 
                             width=prefs["layout.browser_width"],
                             before=self.center_pw)
        elif not prefs["layout.browser_visible"] and self.browse_book.winfo_ismapped():
            if adjust_window_width:
                self._check_update_window_width(-prefs["layout.browser_width"]-ui_utils.SASHTHICKNESS)
            self.main_pw.remove(self.browse_book)

            self.step_over = False

    def cmd_update_memory_visibility(self, adjust_window_width=True):
        # TODO: treat variables frame and memory pane differently
        if prefs["layout.memory_visible"] and not self.right_pw.winfo_ismapped():
            if adjust_window_width:
                self._check_update_window_width(+prefs["layout.memory_width"]+ui_utils.SASHTHICKNESS)
            
            self.main_pw.add(self.right_pw, minsize=150, 
                             width=prefs["layout.memory_width"],
                             after=self.center_pw)
        elif not prefs["layout.memory_visible"] and self.right_pw.winfo_ismapped():
            if adjust_window_width:
                self._check_update_window_width(-prefs["layout.memory_width"]-ui_utils.SASHTHICKNESS)
            self.main_pw.remove(self.right_pw)
            

    def cmd_update_inspector_visibility(self):
        if prefs["layout.inspector_visible"]:
            self.right_pw.add(self.info_book, minsize=50)
        else:
            self.right_pw.remove(self.info_book)

    def cmd_update_outline_visibility_enabled(self):
        return self.editor_book.get_current_editor() is not None

    def cmd_update_outline_visibility(self): 
        if not self.outline_frame.outline_shown:
            self.outline_frame.outline_shown = True 
            self.outline_frame.parse_and_display_module(self.editor_book.get_current_editor()._code_view)
            self.right_pw.add(self.outline_book, minsize=50)
        else:
            self.outline_frame.prepare_for_removal()
            self.outline_frame.outline_shown = False
            self.right_pw.remove(self.outline_book)

    def cmd_refactor_rename_enabled(self):
	    return self.editor_book.get_current_editor() is not None

    def cmd_refactor_rename(self):
        if not self.editor_book.get_current_editor():
            errorMessage = tkMessageBox.showerror(
                           title="Rename failed",
                           message="Rename operation failed (no active editor tabs?).", #TODO - more informative text needed
                           master=self)
            return		

        #create a list of active but unsaved/modified editors)
        unsaved_editors = [x for x in self.editor_book.winfo_children() if x.cmd_save_file_enabled()]
		
        if len(unsaved_editors) != 0:
            #confirm with the user that all open editors need to be saved first
            confirm = tkMessageBox.askyesno(
                      title="Save Files Before Rename",
                      message="All modified files need to be saved before refactoring. Do you want to continue?",
                      default=tkMessageBox.YES,
                      master=self)

            if not confirm:
                return #if user doesn't want it, return

            for editor in unsaved_editors:                     
                if not editor.get_filename():
                    self.editor_book.select(editor) #in the case of editors with no filename, show it, so user know which one they're saving
                editor.cmd_save_file()
                if editor.cmd_save_file_enabled(): #just a sanity check - if after saving a file still needs saving, something is wrong
                    errorMessage = tkMessageBox.showerror(
                                   title="Rename failed",
                                   message="Rename operation failed (saving file failed).", #TODO - more informative text needed
                                   master=self)
                    return

        filename = self.editor_book.get_current_editor().get_filename()

        if filename == None: #another sanity check - the current editor should have an associated filename by this point 
            errorMessage = tkMessageBox.showerror(
                           title="Rename failed",
                           message="Rename operation failed (no filename associated with current module).", #TODO - more informative text needed
                           master=self)
            return			

        identifier = re.compile(r"^[^\d\W]\w*\Z", re.UNICODE) #regex to compare valid python identifiers against
		
        while True: #ask for new variable name until a valid one is entered
            renameWindow = thonny.refactor.RenameWindow(self)
            newname = renameWindow.refactor_new_variable_name
            if newname == None:  
                return #user canceled, return
			
            if re.match(identifier, newname):
                break #valid identifier entered, continue

            errorMessage = tkMessageBox.showerror(
                           title="Incorrect identifier",
                           message="Incorrect Python identifier, please re-enter.",
                           master=self)				

        try: 
            #calculate the offset for rope
            offset = thonny.refactor.calculate_offset(self.editor_book.get_current_editor()._code_view.text)
            #get the project handle and list of changes
            project, changes = thonny.refactor.get_list_of_rename_changes(filename, newname, offset)
            #if len(changes.changes == 0): raise Exception

        except Exception:
            try: #rope needs the cursor to be AFTER the first character of the variable being refactored
                 #so the reason for failure might be that the user had the cursor before the variable name
                offset = offset + 1
                project, changes = thonny.refactor.get_list_of_rename_changes(filename, newname, offset)
                #if len(changes.changes == 0): raise Exception

            except Exception: #user is trying the operation on a non-identifier, let's return
                errorMessage = tkMessageBox.showerror(
                               title="Rename failed",
                               message="Rename operation failed (not a valid Python identifier selected?).", #TODO - more informative text needed
                               master=self)               
                return
		
        #sanity check
        if len(changes.changes) == 0:
            errorMessage = tkMessageBox.showerror(
                               title="Rename failed",
                               message="Rename operation failed - no identifiers affected by change.", #TODO - more informative text needed
                               master=self)               
            return
			
        #show the preview window to user
        messageText = 'Confirm the changes. The following files will be modified:\n'
        for change in changes.changes:
            messageText += '\n ' + change.resource._path

        messageText += '\n\n NB! This action cannot be undone.'

        confirm = tkMessageBox.askyesno(
                  title="Confirm changes",
                  message=messageText,
                  default=tkMessageBox.YES,
                  master=self)
        
        #confirm with user to finalize the changes
        if not confirm:
            thonny.refactor.cancel_changes(project)
            return

        try:
            thonny.refactor.perform_changes(project, changes)			
        except Exception:
                errorMessage = tkMessageBox.showerror(
                               title="Rename failed",
                               message="Rename operation failed (Rope error).", #TODO - more informative text needed
                               master=self)     
                thonny.refactor.cancel_changes(project)							   
                return            
	   
        #everything went fine, let's load all the active tabs again and set their content
        for editor in self.editor_book.winfo_children():
            try: 
                filename = editor.get_filename()
                source, self.file_encoding = misc_utils.read_python_file(filename)
                editor._code_view.set_content(source)
                editor._code_view.modified_since_last_save = False
                self.editor_book.tab(editor, text=self.editor_book._generate_editor_title(filename))
            except Exception:
                try: #it is possible that a file (module) itself was renamed - Rope allows it. so let's see if a file exists with the new name. 
                    filename = filename.replace(os.path.split(filename)[1], newname + '.py')
                    source, self.file_encoding = misc_utils.read_python_file(filename)
                    editor._code_view.set_content(source)
                    editor._code_view.modified_since_last_save = False
                    self.editor_book.tab(editor, text=self.editor_book._generate_editor_title(filename))
                except Exception: #something went wrong with reloading the file, let's close this tab to avoid consistency problems
                    self.editor_book.forget(editor)
                    editor.destroy()					

    def _check_update_window_width(self, delta):
        if not ui_utils.get_zoomed(self):
            self.update_idletasks()
            # TODO: shift to left if right edge goes away from screen
            # TODO: check with screen width
            new_geometry = "{0}x{1}+{2}+{3}".format(self.winfo_width() + delta,
                                                   self.winfo_height(),
                                                   self.winfo_x(), self.winfo_y())
            
            self.geometry(new_geometry)
            
    
    def cmd_update_memory_model_enabled(self):
        return self._vm.get_state() == "toplevel"
    
    def cmd_update_memory_model(self):
        if prefs["values_in_heap"] and not self.heap_book.winfo_ismapped():
            # TODO: put it before object info block
            self.right_pw.add(self.heap_book, after=self.globals_book, minsize=50)
        elif not prefs["values_in_heap"] and self.heap_book.winfo_ismapped():
            self.right_pw.remove(self.heap_book)
        
        self.globals_frame.update_memory_model()
        self.inspector_frame.update_memory_model()
        # TODO: globals and locals
        
        assert self._vm.get_state() == "toplevel"
        # TODO: following command creates an unnecessary new propmpt
        self._vm.send_command(ToplevelCommand(command="pass", heap_required=True))

    def cmd_update_debugging_mode(self):
        print(prefs["advanced_debugging"])

    def cmd_mac_add_download_assessment(self):
        # TODO:
        """
        Normally Mac doesn't allow opening py files from web directly
        See:
        http://keith.chaos-realm.net/plugin/tag/downloadassessment
        https://developer.apple.com/library/mac/#documentation/Miscellaneous/Reference/UTIRef/Articles/System-DeclaredUniformTypeIdentifiers.html
        
        create file ~/Library/Preferences/com.apple.DownloadAssessment.plist
        with following content:
        
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>LSRiskCategorySafe</key> 
            <dict>
                <key>LSRiskCategoryContentTypes</key>
                <array>
                    <string>public.xml</string>
                </array>
            
                <key>LSRiskCategoryExtensions</key>
                <array>
                    <string>py</string>
                    <string>pyw</string>
                </array>
            </dict>
        </dict>
        </plist>
        """

    def cmd_step_enabled(self):
        #self._check_issue_goto_before_or_after()
        msg = self._vm.get_state_message()
        # always enabled during debugging
        return (isinstance(msg, DebuggerResponse)) 
    
    def cmd_step(self, automatic=False):
        if not automatic:
            self.step_over = False
        self._check_issue_debugger_command(DebuggerCommand(command="step"))
    
    def cmd_zoom_enabled(self):
        #self._check_issue_goto_before_or_after()
        return self.cmd_exec_enabled()
    
    def cmd_zoom(self):
        self._check_issue_debugger_command(DebuggerCommand(command="zoom"))
    
    def cmd_exec_enabled(self):
        return self.cmd_step_enabled()
        #self._check_issue_goto_before_or_after()
        #msg = self._vm.get_state_message()
        #return (isinstance(msg, DebuggerResponse) 
        #        and msg.state in ("before_expression", "before_expression_again",
        #                          "before_statement", "before_statement_again")) 
    
    def cmd_exec(self):
        self.step_over = True
        self.cmd_step(True)
    
    def cmd_focus_editor(self):
        self.editor_book.focus_current_editor()
    
    def cmd_focus_shell(self):
        self.shell.focus_set()
    
    def cmd_open_user_dir(self):
        misc_utils.open_path_in_system_file_manager(THONNY_USER_DIR)
    
    def _check_issue_debugger_command(self, cmd, automatic=False):
        if isinstance(self._vm.get_state_message(), DebuggerResponse):
            self._issue_debugger_command(cmd, automatic)
            # TODO: notify memory panes and editors? Make them inactive?
    
    def _issue_debugger_command(self, cmd, automatic=False):
        print("_issue", cmd, automatic)
        last_response = self._vm.get_state_message()
        # tell VM the state we are seeing
        cmd.setdefault (
            frame_id=last_response.frame_id,
            state=last_response.state,
            focus=last_response.focus,
            heap_required=prefs["values_in_heap"]
        )
        
        if not automatic:
            self.last_manual_debugger_command_sent = cmd    # TODO: hack
        self._vm.send_command(cmd)
        # TODO: notify memory panes and editors? Make them inactive?
            
    def cmd_set_auto_cd(self):
        print(self._auto_cd.get())
        
    def stop_debugging(self):
        self.editor_book.stop_debugging()
        self.shell.stop_debugging()
        self.globals_frame.stop_debugging()
        self.builtins_frame.stop_debugging()
        self.heap_frame.stop_debugging()
        self._vm.reset()
    
    def start_debugging(self, filename=None):
        self.editor_book.start_debugging(self._vm, filename)
        self.shell.start_debugging(self._vm, filename)
        self._vm.start()
    
    
    def _update_menu(self, name, menu_widget):
        for menu_name, _, items in self._menus:
            if menu_name == name:
                for item in items:
                    if isinstance(item, Command):
                        if item.is_enabled():
                            menu_widget.entryconfigure(item.label, state=tk.NORMAL)
                        else:
                            menu_widget.entryconfigure(item.label, state=tk.DISABLED)
                    
                    
        
    
    def _find_current_edit_widget(self):
        ""
        widget = self.focus_get()
        if (isinstance(widget, tk.Text) and widget.cget("undo")):
            return widget.master
            
            
            

    def cmd_show_ast(self):
        if not notebook_contains(self.control_book, self.ast_frame): 
            self.control_book.add(self.ast_frame, text="AST")
        self.ast_frame.show_ast(self.editor_book.get_current_editor()._code_view)
        self.control_book.select(self.ast_frame)
    
    def cmd_show_replayer(self):
        launcher = os.path.join(self.src_dir, "replay")
        cmd_line = [sys.executable, '-u', launcher]
        subprocess.Popen(cmd_line)
    
    def _get_version(self):
        try:
            with open(os.path.join(os.path.dirname(__file__), "VERSION")) as fp:
                return StrictVersion(fp.read().strip())
        except:
            return StrictVersion("0.0")
      
    def _update_title(self):
        self.title("Thonny  -  Python {1}.{2}.{3}  -  {0}".format(self._vm.cwd, *sys.version_info))
    
    def _mac_open_document(self, *args):
        # TODO:
        #showinfo("open doc", str(args))
        pass
    
    def _mac_open_application(self, *args):
        #showinfo("open app", str(args))
        pass
    
    def _mac_reopen_application(self, *args):
        #showinfo("reopen app", str(args))
        pass
    
    def _on_close(self):
        if not self.editor_book.check_allow_closing():
            return
        
        try:
            self._save_preferences()
            self.user_logger.save()
            ui_utils.delete_images()
        except:
            tk.messagebox.showerror("Internal error. Use Ctrl+C to copy",
                                traceback.format_exc())
        
        self.destroy()
        
        
    def _save_preferences(self):
        self.update_idletasks()
        
        # update layout prefs
        if self.browse_book.winfo_ismapped():
            prefs["layout.browser_width"] = self.browse_book.winfo_width()
        
        if self.right_pw.winfo_ismapped():
            prefs["layout.memory_width"] = self.right_pw.winfo_width()
            # TODO: heigths
        
        prefs["layout.zoomed"] = ui_utils.get_zoomed(self)
        if not ui_utils.get_zoomed(self):
            prefs["layout.top"] = self.winfo_y()
            prefs["layout.left"] = self.winfo_x()
            prefs["layout.width"] = self.winfo_width()
            prefs["layout.height"] = self.winfo_height()
        
        center_width = self.center_pw.winfo_width();
        if center_width > 1:
            prefs["layout.center_width"] = center_width
        
        if self.right_pw.winfo_ismapped():
            prefs["layout.memory_width"] = self.right_pw.winfo_width()
        
        if self.browse_book.winfo_ismapped():
            prefs["layout.browser_width"] = self.browse_book.winfo_width()
            
        prefs.save()
    
    
    def new_user_log_file(self):
        folder = os.path.expanduser(os.path.join("~", ".thonny", "user_logs"))
        if not os.path.exists(folder):
            os.makedirs(folder)
            
        i = 0
        while True: 
            fname = os.path.join(folder, time.strftime("%Y-%m-%d_%H-%M-%S_{}.txt".format(i)));
            if os.path.exists(fname):
                i += 1;  
            else:
                return fname

    def on_get_focus(self, e):
        user_logging.log_user_event(user_logging.ProgramGetFocusEvent());

    def on_lose_focus(self, e):
        user_logging.log_user_event(user_logging.ProgramLoseFocusEvent());
        
    
    def on_tk_exception(self, exc, val, tb):
        # copied from tkinter.Tk.report_callback_exception
        # Aivar: following statement kills the process when run with pythonw.exe
        # http://bugs.python.org/issue22384
        #sys.stderr.write("Exception in Tkinter callback\n")
        sys.last_type = exc
        sys.last_value = val
        sys.last_traceback = tb
        traceback.print_exception(exc, val, tb)
        
        # TODO: Command+C for Mac
        tk.messagebox.showerror("Internal error. Use Ctrl+C to copy",
                                traceback.format_exc())
    
    def set_icon(self):
        try:
            self.iconbitmap(default=os.path.join(self.src_dir, "res", "thonny.ico"))
        except:
            pass

def launch():
    try:
        if not os.path.exists(THONNY_USER_DIR):
            os.makedirs(THONNY_USER_DIR, 0o700)
        Thonny().mainloop()
    except:
        traceback.print_exc()
        # TODO: Command+C for Mac
        tk.messagebox.showerror("Internal error. Program will close. Use Ctrl+C to copy",
                                traceback.format_exc())
    


