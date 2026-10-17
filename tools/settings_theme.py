"""Complete dark control palette for embedded and standalone settings windows."""
from tkinter import ttk

BG, FIELD, DISABLED = '#171b23', '#303949', '#252b35'
TEXT, MUTED, EDGE = '#eeeae0', '#a6adbb', '#53627b'
SELECT, GOLD = '#4c6486', '#f4e5c5'


def apply_settings_theme(root):
    style = ttk.Style(root)
    # Native Windows entry/tab elements ignore parts of the dark palette.
    if style.theme_use() != 'clam':
        style.theme_use('clam')
    root.configure(bg=BG)
    style.configure('TFrame', background=BG)
    style.configure('TLabel', background=BG, foreground=TEXT, font=('Microsoft YaHei UI', 10))
    style.configure('TButton', background=FIELD, foreground=TEXT, padding=(13, 9), font=('Microsoft YaHei UI', 10))
    style.map('TButton', background=[('disabled', DISABLED), ('active', '#44536c')], foreground=[('disabled', MUTED)])
    for control in ('TEntry', 'TCombobox'):
        style.configure(control, fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                        insertcolor=TEXT, bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE,
                        selectbackground=SELECT, selectforeground='#ffffff', arrowcolor=GOLD, padding=3)
        style.map(control, fieldbackground=[('disabled', DISABLED), ('readonly', FIELD)],
                  background=[('disabled', DISABLED), ('active', '#44536c'), ('!disabled', FIELD)],
                  foreground=[('disabled', MUTED), ('readonly', TEXT)],
                  selectbackground=[('!disabled', SELECT)], selectforeground=[('!disabled', '#ffffff')],
                  bordercolor=[('focus', '#8dafff'), ('disabled', '#343e50')],
                  arrowcolor=[('disabled', MUTED), ('!disabled', GOLD)])
    style.configure('TNotebook', background=BG, bordercolor=EDGE, tabmargins=(0, 2, 0, 0))
    style.configure('TNotebook.Tab', background='#222834', foreground='#c3cbd8',
                    bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE, padding=(14, 8))
    style.map('TNotebook.Tab', background=[('selected', '#354866'), ('active', FIELD)],
              foreground=[('disabled', MUTED), ('selected', GOLD), ('active', TEXT)])
    style.configure('TCheckbutton', background=BG, foreground=TEXT, padding=4)
    style.map('TCheckbutton', background=[('active', BG)], foreground=[('disabled', MUTED)])
    style.configure('Settings.Treeview', background='#222834', fieldbackground='#222834',
                    foreground=TEXT, rowheight=32, bordercolor=EDGE)
    style.configure('Settings.Treeview.Heading', background=FIELD, foreground=TEXT, padding=(6, 8))
    style.map('Settings.Treeview', background=[('selected', SELECT)], foreground=[('selected', '#ffffff')])
    root.option_add('*TCombobox*Listbox.background', FIELD)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', SELECT)
    root.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
    return style
