#!/usr/bin/env python3
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import socket, queue, codecs

from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
import PyQt5.QtCore as QtCore

from electroncash.i18n import _, pgettext
from electroncash import networks
from electroncash.util import print_error, Weak, PrintError
from electroncash.network import serialize_server, deserialize_server, get_eligible_servers

from electroncash.slp_validator_0x01 import shared_context

from .util import *

protocol_names = ['TCP', 'SSL']
protocol_letters = 'ts'

class NetworkDialog(QDialog, MessageBoxMixin):
    network_updated_signal = pyqtSignal()

    def __init__(self, network, config):
        QDialog.__init__(self)
        self.setWindowTitle(_('Network'))
        self.setMinimumSize(500, 350)
        self.nlayout = NetworkChoiceLayout(self, network, config)
        vbox = QVBoxLayout(self)
        vbox.addLayout(self.nlayout.layout())
        vbox.addLayout(Buttons(CloseButton(self)))
        self.network_updated_signal.connect(self.on_update)
        network.register_callback(self.on_network, ['blockchain_updated', 'interfaces', 'status'])

    def on_network(self, event, *args):
        ''' This may run in network thread '''
        #print_error("[NetworkDialog] on_network:",event,*args)
        self.network_updated_signal.emit() # this enqueues call to on_update in GUI thread

    @rate_limited(0.333) # limit network window updates to max 3 per second. More frequent isn't that useful anyway -- and on large wallets/big synchs the network spams us with events which we would rather collapse into 1
    def on_update(self):
        ''' This always runs in main GUI thread '''
        self.nlayout.update()

    def closeEvent(self, e):
        # Warn if non-SSL mode when closing dialog
        if (not self.nlayout.ssl_cb.isChecked()
                and not self.nlayout.tor_cb.isChecked()
                and not self.nlayout.server_host.text().lower().endswith('.onion')
                and not self.nlayout.config.get('non_ssl_noprompt', False)):
            ok, chk = self.question(''.join([_("You have selected non-SSL mode for your server settings."), ' ',
                                             _("Using this mode presents a potential security risk."), '\n\n',
                                             _("Are you sure you wish to proceed?")]),
                                    detail_text=''.join([
                                             _("All of your traffic to the blockchain servers will be sent unencrypted."), ' ',
                                             _("Additionally, you may also be vulnerable to man-in-the-middle attacks."), ' ',
                                             _("It is strongly recommended that you go back and enable SSL mode."),
                                             ]),
                                    rich_text=False,
                                    title=_('Security Warning'),
                                    icon=QMessageBox.Critical,
                                    checkbox_text=("Don't ask me again"))
            if chk: self.nlayout.config.set_key('non_ssl_noprompt', True)
            if not ok:
                e.ignore()
                return
        super().closeEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        QDialog.update(self)  # hax to work around Ubuntu 16 bugs -- James Cramer observed that if this isn't here dialog sometimes doesn't paint properly on show.



class NodesListWidget(QTreeWidget):

    def __init__(self, parent):
        QTreeWidget.__init__(self)
        self.parent = parent
        self.setHeaderLabels([_('Connected node'), _('Height')])
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.create_menu)

    def create_menu(self, position):
        item = self.currentItem()
        if not item:
            return
        is_server = not bool(item.data(0, Qt.UserRole))
        menu = QMenu()
        if is_server:
            server = item.data(1, Qt.UserRole)
            menu.addAction(_("Use as server"), lambda: self.parent.follow_server(server))
        else:
            index = item.data(1, Qt.UserRole)
            menu.addAction(_("Follow this branch"), lambda: self.parent.follow_branch(index))
        menu.exec_(self.viewport().mapToGlobal(position))

    def keyPressEvent(self, event):
        if event.key() in [ Qt.Key_F2, Qt.Key_Return ]:
            item, col = self.currentItem(), self.currentColumn()
            if item and col > -1:
                self.on_activated(item, col)
        else:
            QTreeWidget.keyPressEvent(self, event)

    def on_activated(self, item, column):
        # on 'enter' we show the menu
        pt = self.visualItemRect(item).bottomLeft()
        pt.setX(50)
        self.customContextMenuRequested.emit(pt)

    def update(self, network):
        self.clear()
        self.addChild = self.addTopLevelItem
        chains = network.get_blockchains()
        n_chains = len(chains)
        for k, items in chains.items():
            b = network.blockchains[k]
            name = b.get_name()
            if n_chains >1:
                x = QTreeWidgetItem([name + '@%d'%b.get_base_height(), '%d'%b.height()])
                x.setData(0, Qt.UserRole, 1)
                x.setData(1, Qt.UserRole, b.base_height)
            else:
                x = self
            for i in items:
                star = ' ◀' if i == network.interface else ''
                item = QTreeWidgetItem([i.host + star, '%d'%i.tip])
                item.setData(0, Qt.UserRole, 0)
                item.setData(1, Qt.UserRole, i.server)
                x.addChild(item)
            if n_chains>1:
                self.addTopLevelItem(x)
                x.setExpanded(True)

        h = self.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)

class ServerFlag:
    ''' Used by ServerListWidget for Server flags & Symbols '''
    Banned = 2 # Blacklisting/banning was a hidden mechanism inherited from Electrum. We would blacklist misbehaving servers under the hood. Now that facility is exposed (editable by the user). We never connect to blacklisted servers.
    Preferred = 1 # Preferred servers (white-listed) start off as the servers in servers.json and are "more trusted" and optionally the user can elect to connect to only these servers
    NoFlag = 0
    Symbol = ("", "★", "⛔") # indexed using pseudo-enum above
    UnSymbol = ("", "✖", "⚬") # used for "disable X" context menu

class ServerListWidget(QTreeWidget):

    def __init__(self, parent):
        QTreeWidget.__init__(self)
        self.parent = parent
        self.setHeaderLabels(['', _('Host'), _('Port')])
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.create_menu)

    def create_menu(self, position):
        item = self.currentItem()
        if not item:
            return
        menu = QMenu()
        server = item.data(2, Qt.UserRole)
        if self.parent.can_set_server(server):
            useAction = menu.addAction(_("Use as server"), lambda: self.set_server(server))
        else:
            useAction = menu.addAction(server.split(':',1)[0], lambda: None)
            useAction.setDisabled(True)
        menu.addSeparator()
        flagval = item.data(0, Qt.UserRole)
        iswl = flagval & ServerFlag.Preferred
        if flagval & ServerFlag.Banned:
            optxt = ServerFlag.UnSymbol[ServerFlag.Banned] + " " + _("Unban server")
            isbl = True
            useAction.setDisabled(True)
            useAction.setText(_("Server banned"))
        else:
            optxt = ServerFlag.Symbol[ServerFlag.Banned] + " " + _("Ban server")
            isbl = False
            if not isbl:
                if flagval & ServerFlag.Preferred:
                    optxt_fav = ServerFlag.UnSymbol[ServerFlag.Preferred] + " " + _("Remove from preferred")
                else:
                    optxt_fav = ServerFlag.Symbol[ServerFlag.Preferred] + " " + _("Add to preferred")
                menu.addAction(optxt_fav, lambda: self.parent.set_whitelisted(server, not iswl))
        menu.addAction(optxt, lambda: self.parent.set_blacklisted(server, not isbl))
        menu.exec_(self.viewport().mapToGlobal(position))

    def set_server(self, s):
        host, port, protocol = deserialize_server(s)
        self.parent.server_host.setText(host)
        self.parent.server_port.setText(port)
        self.parent.autoconnect_cb.setChecked(False) # force auto-connect off if they did "Use as server"
        self.parent.set_server()
        self.parent.update()

    def keyPressEvent(self, event):
        if event.key() in [ Qt.Key_F2, Qt.Key_Return ]:
            item, col = self.currentItem(), self.currentColumn()
            if item and col > -1:
                self.on_activated(item, col)
        else:
            QTreeWidget.keyPressEvent(self, event)

    def on_activated(self, item, column):
        # on 'enter' we show the menu
        pt = self.visualItemRect(item).bottomLeft()
        pt.setX(50)
        self.customContextMenuRequested.emit(pt)

    @staticmethod
    def lightenItemText(item, rang=None):
        if rang is None: rang = range(0, item.columnCount())
        for i in rang:
            brush = item.foreground(i); color = brush.color(); color.setHsvF(color.hueF(), color.saturationF(), 0.5); brush.setColor(color)
            item.setForeground(i, brush)

    def update(self, network, servers, protocol, use_tor):
        self.clear()
        self.setIndentation(0)
        wl_only = network.is_whitelist_only()
        for _host, d in sorted(servers.items()):
            if _host.lower().endswith('.onion') and not use_tor:
                continue
            port = d.get(protocol)
            if port:
                server = serialize_server(_host, port, protocol)
                flag, flagval, tt = (ServerFlag.Symbol[ServerFlag.Banned], ServerFlag.Banned, _("This server is banned")) if network.server_is_blacklisted(server) else ("", 0, "")
                flag2, flagval2, tt2 = (ServerFlag.Symbol[ServerFlag.Preferred], ServerFlag.Preferred, _("This is a preferred server")) if network.server_is_whitelisted(server) else ("", 0, "")
                flag = flag or flag2; del flag2
                tt = tt or tt2; del tt2
                flagval |= flagval2; del flagval2
                x = QTreeWidgetItem([flag, _host, port])
                if tt: x.setToolTip(0, tt)
                if (wl_only and flagval != ServerFlag.Preferred) or flagval & ServerFlag.Banned:
                    # lighten the text of servers we can't/won't connect to for the given mode
                    self.lightenItemText(x, range(1,3))
                x.setData(2, Qt.UserRole, server)
                x.setData(0, Qt.UserRole, flagval)
                self.addTopLevelItem(x)

        h = self.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)

class SlpSearchJobListWidget(QTreeWidget):
    def __init__(self, parent):
        QTreeWidget.__init__(self)
        self.parent = parent
        self.network = parent.network
        self.setHeaderLabels([_("Job Id"), _("Txn Count"), _("Data"), _("Status")])
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.create_menu)
        self.slp_validity_signal = None
        self.slp_validation_fetch_signal = None

    def on_validation_fetch(self, total_data_received):
        if total_data_received > 0:
            self.parent.data_label.setText(self.humanbytes(total_data_received))
        self.update()

    def create_menu(self, position):
        item = self.currentItem()
        if not item:
            return
        menu = QMenu()
        menu.addAction(_("Copy Txid"), lambda: self._copy_txid_to_clipboard())
        menu.addAction(_("Copy Reversed Txid"), lambda: self._copy_txid_to_clipboard(True))
        menu.addAction(_("Refresh List"), lambda: self.update())
        txid = item.data(0, Qt.UserRole)
        if item.data(3, Qt.UserRole) in ['Exited']:
            menu.addAction(_("Restart Search"), lambda: self.restart_job(txid))
        elif item.data(3, Qt.UserRole) not in ['Exited', 'Downloaded']:
            menu.addAction(_("Cancel"), lambda: self.cancel_job(txid))
        menu.exec_(self.viewport().mapToGlobal(position))

    def _copy_txid_to_clipboard(self, flip_bytes=False):
        txid = self.currentItem().data(0, Qt.UserRole)
        if flip_bytes:
            txid = codecs.encode(codecs.decode(txid,'hex')[::-1], 'hex').decode()
        qApp.clipboard().setText(txid)

    def restart_job(self, txid):
        job = shared_context.graph_search_mgr.search_jobs.get(txid)
        if job:
            shared_context.graph_search_mgr.restart_search(job)
        self.update()

    def cancel_job(self, txid):
        job = shared_context.graph_search_mgr.search_jobs.get(txid)
        if job:
            job.sched_cancel(reason='user cancelled')

    def keyPressEvent(self, event):
        if event.key() in [ Qt.Key_F2, Qt.Key_Return ]:
            item, col = self.currentItem(), self.currentColumn()
            if item and col > -1:
                self.on_activated(item, col)
        else:
            QTreeWidget.keyPressEvent(self, event)

    def on_activated(self, item, column):
        # on 'enter' we show the menu
        pt = self.visualItemRect(item).bottomLeft()
        pt.setX(50)
        self.customContextMenuRequested.emit(pt)

    @staticmethod
    def humanbytes(B):
        'Return the given bytes as a human friendly KB, MB, GB, or TB string'
        B = float(B)
        KB = float(1024)
        MB = float(KB ** 2) # 1,048,576
        GB = float(KB ** 3) # 1,073,741,824
        TB = float(KB ** 4) # 1,099,511,627,776

        if B < KB:
            return '{0} {1}'.format(B,'Bytes' if 0 == B > 1 else 'Byte')
        elif KB <= B < MB:
            return '{0:.2f} KB'.format(B/KB)
        elif MB <= B < GB:
            return '{0:.2f} MB'.format(B/MB)
        elif GB <= B < TB:
            return '{0:.2f} GB'.format(B/GB)
        elif TB <= B:
            return '{0:.2f} TB'.format(B/TB)

    @rate_limited(1.0, classlevel=True, ts_after=True) # We rate limit the refresh no more than 1 times every second
    def update(self):
        self.parent.slp_gs_enable_cb.setChecked(self.parent.config.get('slp_validator_graphsearch_enabled', False))
        selected_item_id = self.currentItem().data(0, Qt.UserRole) if self.currentItem() else None
        if not self.slp_validation_fetch_signal and self.parent.network.slp_validation_fetch_signal:
            self.slp_validation_fetch_signal = self.parent.network.slp_validation_fetch_signal
            self.slp_validation_fetch_signal.connect(self.on_validation_fetch, Qt.QueuedConnection)
        self.clear()
        jobs = shared_context.graph_search_mgr.search_jobs.copy()
        working_item = None
        completed_items = []
        other_items = []
        for k, job in jobs.items():
            if len(jobs) > 0:
                tx_count = str(job.txn_count_progress)
                status = 'In Queue'
                if job.search_success:
                    status = 'Downloaded'
                elif job.job_complete:
                    status = 'Exited'
                elif job.waiting_to_cancel:
                    status = 'Stopping...'
                elif job.search_started:
                    status = 'Downloading...'
                success = str(job.search_success) if job.search_success else ''
                exit_msg = ' ('+job.exit_msg+')' if job.exit_msg and status != 'Downloaded' else ''
                x = QTreeWidgetItem([job.root_txid[:6], tx_count, self.humanbytes(job.gs_response_size), status + exit_msg])
                x.setData(0, Qt.UserRole, k)
                x.setData(3, Qt.UserRole, status)
                if status == 'Downloading...':
                    working_item = x
                elif status == "Downloaded":
                    completed_items.append(x)
                else:
                    other_items.append(x)

        def setCurrentSelectedItem(i):
            if selected_item_id and i.data(0, Qt.UserRole) == selected_item_id:
                self.setCurrentItem(i)

        if completed_items:
            for i in completed_items[::-1]:
                self.addTopLevelItem(i)
                setCurrentSelectedItem(i)
        if other_items:
            for i in other_items:
                self.addTopLevelItem(i)
                setCurrentSelectedItem(i)
        if working_item:
            self.insertTopLevelItem(0, working_item)
            setCurrentSelectedItem(working_item)

        h = self.header()
        h.setStretchLastSection(True)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)

class SlpGsServeListWidget(QTreeWidget):
    def __init__(self, parent):
        QTreeWidget.__init__(self)
        self.parent = parent
        self.network = parent.network
        self.setHeaderLabels([_('GS Server')]) #, _('Server Status')])
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.create_menu)
        host = self.parent.config.get('slp_gs_host', None)
        if not host and networks.net.SLPDB_SERVERS:  # Note: testnet4 and scalenet may have empty SLPDB_SERVERS
            host = next(iter(networks.net.SLPDB_SERVERS))
            self.parent.config.set_key('slp_gs_host', host)
        self.network.slp_gs_host = host

    def create_menu(self, position):
        item = self.currentItem()
        if not item:
            return
        menu = QMenu()
        server = item.data(0, Qt.UserRole)
        menu.addAction(_("Use as server"), lambda: self.select_slp_gs_server(server))
        menu.exec_(self.viewport().mapToGlobal(position))

    def select_slp_gs_server(self, server):
        self.parent.set_slp_server(server)
        self.update()

    def keyPressEvent(self, event):
        if event.key() in [ Qt.Key_F2, Qt.Key_Return ]:
            item, col = self.currentItem(), self.currentColumn()
            if item and col > -1:
                self.on_activated(item, col)
        else:
            QTreeWidget.keyPressEvent(self, event)

    def on_activated(self, item, column):
        # on 'enter' we show the menu
        pt = self.visualItemRect(item).bottomLeft()
        pt.setX(50)
        self.customContextMenuRequested.emit(pt)

    def update(self):
        self.clear()
        self.addChild = self.addTopLevelItem
        slp_gs_list = networks.net.SLPDB_SERVERS
        slp_gs_count = len(slp_gs_list)
        for k, items in slp_gs_list.items():
            if slp_gs_count > 0:
                star = ' ◀' if k == self.network.slp_gs_host else ''
                x = QTreeWidgetItem([k+star]) #, 'NA'])
                x.setData(0, Qt.UserRole, k)
                # x.setData(1, Qt.UserRole, k)
                self.addTopLevelItem(x)
        h = self.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        #h.setSectionResizeMode(1, QHeaderView.ResizeToContents)

class NetworkChoiceLayout(QObject, PrintError):

    def __init__(self, parent, network, config, wizard=False):
        super().__init__(parent)
        self.network = network
        self.config = config
        self.protocol = None
        self.tor_proxy = None

        # tor detector
        self.td = TorDetector(self)
        self.td.found_proxy.connect(self.suggest_proxy)

        self.tabs = tabs = QTabWidget()
        server_tab = QWidget()
        weakTd = Weak.ref(self.td)
        class ProxyTab(QWidget):
            def showEvent(slf, e):
                super().showEvent(e)
                td = weakTd()
                if e.isAccepted() and td:
                    td.start() # starts the tor detector when proxy_tab appears
            def hideEvent(slf, e):
                super().hideEvent(e)
                td = weakTd()
                if e.isAccepted() and td:
                    td.stop() # stops the tor detector when proxy_tab disappears
        proxy_tab = ProxyTab()
        blockchain_tab = QWidget()
        slp_tab = QWidget()
        tabs.addTab(blockchain_tab, _('Overview'))
        tabs.addTab(server_tab, _('Server'))
        tabs.addTab(proxy_tab, _('Proxy'))
        tabs.addTab(slp_tab, _('Tokens'))

        if wizard:
            tabs.setCurrentIndex(1)

        # server tab
        grid = QGridLayout(server_tab)
        grid.setSpacing(8)

        self.server_host = QLineEdit()
        self.server_host.setFixedWidth(200)
        self.server_port = QLineEdit()
        self.server_port.setFixedWidth(60)
        self.ssl_cb = QCheckBox(_('Use SSL'))
        self.autoconnect_cb = QCheckBox(_('Select server automatically'))
        self.autoconnect_cb.setEnabled(self.config.is_modifiable('auto_connect'))

        weakSelf = Weak.ref(self)  # Qt/Python GC hygeine: avoid strong references to self in lambda slots.
        self.server_host.editingFinished.connect(lambda: weakSelf() and weakSelf().set_server(onion_hack=True))
        self.server_port.editingFinished.connect(lambda: weakSelf() and weakSelf().set_server(onion_hack=True))
        self.ssl_cb.clicked.connect(self.change_protocol)
        self.autoconnect_cb.clicked.connect(self.set_server)
        self.autoconnect_cb.clicked.connect(self.update)

        msg = ' '.join([
            _("If auto-connect is enabled, Electron Cash will always use a server that is on the longest blockchain."),
            _("If it is disabled, you have to choose a server you want to use. Electron Cash will warn you if your server is lagging.")
        ])
        grid.addWidget(self.autoconnect_cb, 0, 0, 1, 3)
        grid.addWidget(HelpButton(msg), 0, 4)

        self.preferred_only_cb = QCheckBox(_("Connect only to preferred servers"))
        self.preferred_only_cb.setEnabled(self.config.is_modifiable('whitelist_servers_only'))
        self.preferred_only_cb.setToolTip(_("If enabled, restricts Electron Cash to connecting to servers only marked as 'preferred'."))

        self.preferred_only_cb.clicked.connect(self.set_whitelisted_only) # re-set the config key and notify network.py

        msg = '\n\n'.join([
            _("If 'Connect only to preferred servers' is enabled, Electron Cash will only connect to servers marked as 'preferred' servers ({}).").format(ServerFlag.Symbol[ServerFlag.Preferred]),
            _("This feature was added in response to the potential for a malicious actor to deny service via launching many servers (aka a sybil attack)."),
            _("If unsure, most of the time it's safe to leave this option disabled. However leaving it enabled is safer (if a little bit discouraging to new server operators wanting to populate their servers).")
        ])
        grid.addWidget(self.preferred_only_cb, 1, 0, 1, 3)
        grid.addWidget(HelpButton(msg), 1, 4)


        grid.addWidget(self.ssl_cb, 2, 0, 1, 3)
        self.ssl_help = HelpButton(_('SSL is used to authenticate and encrypt your connections with the blockchain servers.') + "\n\n"
                                   + _('Due to potential security risks, you may only disable SSL when using a Tor Proxy.'))
        grid.addWidget(self.ssl_help, 2, 4)

        grid.addWidget(QLabel(_('Server') + ':'), 3, 0)
        grid.addWidget(self.server_host, 3, 1, 1, 2)
        grid.addWidget(self.server_port, 3, 3)

        self.server_list_label = label = QLabel('') # will get set by self.update()
        grid.addWidget(label, 4, 0, 1, 5)
        self.servers_list = ServerListWidget(self)
        grid.addWidget(self.servers_list, 5, 0, 1, 5)
        self.legend_label = label = WWLabel('') # will get populated with the legend by self.update()
        label.setTextInteractionFlags(label.textInteractionFlags() & (~Qt.TextSelectableByMouse))  # disable text selection by mouse here
        self.legend_label.linkActivated.connect(self.on_view_blacklist)
        grid.addWidget(label, 6, 0, 1, 4)
        msg = ' '.join([
            _("Preferred servers ({}) are servers you have designated as reliable and/or trustworthy.").format(ServerFlag.Symbol[ServerFlag.Preferred]),
            _("Initially, the preferred list is the hard-coded list of known-good servers vetted by the Electron Cash developers."),
            _("You can add or remove any server from this list and optionally elect to only connect to preferred servers."),
            "\n\n"+_("Banned servers ({}) are servers deemed unreliable and/or untrustworthy, and so they will never be connected-to by Electron Cash.").format(ServerFlag.Symbol[ServerFlag.Banned])
        ])
        grid.addWidget(HelpButton(msg), 6, 4)

        # Proxy tab
        grid = QGridLayout(proxy_tab)
        grid.setSpacing(8)

        # proxy setting
        self.proxy_cb = QCheckBox(_('Use proxy'))
        self.proxy_cb.clicked.connect(self.check_disable_proxy)
        self.proxy_cb.clicked.connect(self.set_proxy)

        self.proxy_mode = QComboBox()
        self.proxy_mode.addItems(['SOCKS4', 'SOCKS5', 'HTTP'])
        self.proxy_host = QLineEdit()
        self.proxy_host.setFixedWidth(200)
        self.proxy_port = QLineEdit()
        self.proxy_port.setFixedWidth(60)
        self.proxy_user = QLineEdit()
        self.proxy_user.setPlaceholderText(_("Proxy user"))
        self.proxy_password = QLineEdit()
        self.proxy_password.setPlaceholderText(_("Password"))
        self.proxy_password.setEchoMode(QLineEdit.Password)
        self.proxy_password.setFixedWidth(60)

        self.proxy_mode.currentIndexChanged.connect(self.set_proxy)
        self.proxy_host.editingFinished.connect(self.set_proxy)
        self.proxy_port.editingFinished.connect(self.set_proxy)
        self.proxy_user.editingFinished.connect(self.set_proxy)
        self.proxy_password.editingFinished.connect(self.set_proxy)

        self.proxy_mode.currentIndexChanged.connect(self.proxy_settings_changed)
        self.proxy_host.textEdited.connect(self.proxy_settings_changed)
        self.proxy_port.textEdited.connect(self.proxy_settings_changed)
        self.proxy_user.textEdited.connect(self.proxy_settings_changed)
        self.proxy_password.textEdited.connect(self.proxy_settings_changed)

        self.tor_cb = QCheckBox(_("Use Tor Proxy"))
        self.tor_cb.setIcon(QIcon(":icons/tor_logo.svg"))
        self.tor_cb.setEnabled(False)
        self.tor_cb.clicked.connect(self.use_tor_proxy)

        grid.addWidget(self.tor_cb, 1, 0, 1, 3)
        grid.addWidget(self.proxy_cb, 2, 0, 1, 3)
        grid.addWidget(HelpButton(_('Proxy settings apply to all connections: with Electron Cash servers, but also with third-party services.')), 2, 4)
        grid.addWidget(self.proxy_mode, 4, 1)
        grid.addWidget(self.proxy_host, 4, 2)
        grid.addWidget(self.proxy_port, 4, 3)
        grid.addWidget(self.proxy_user, 5, 2)
        grid.addWidget(self.proxy_password, 5, 3)
        grid.setRowStretch(7, 1)

        # SLP Validation Tab
        grid = QGridLayout(slp_tab)
        self.slp_gs_enable_cb = QCheckBox(_('Use Graph Search server (gs++) to speed up validation'))
        self.slp_gs_enable_cb.clicked.connect(self.use_slp_gs)
        self.slp_gs_enable_cb.setChecked(self.config.get('slp_validator_graphsearch_enabled', False))
        grid.addWidget(self.slp_gs_enable_cb, 0, 0, 1, 3)

        hbox = QHBoxLayout()
        hbox.addWidget(QLabel(_('Server') + ':'))
        self.slp_gs_server_host = QLineEdit()
        self.slp_gs_server_host.setFixedWidth(250)
        self.slp_gs_server_host.editingFinished.connect(lambda: weakSelf() and weakSelf().set_slp_server())
        hbox.addWidget(self.slp_gs_server_host)
        hbox.addStretch(1)
        grid.addLayout(hbox, 1, 0)

        self.slp_gs_list_widget = SlpGsServeListWidget(self)
        grid.addWidget(self.slp_gs_list_widget, 2, 0, 1, 5)
        grid.addWidget(QLabel(_("Current Graph Search Jobs:")), 3, 0)
        self.slp_search_job_list_widget = SlpSearchJobListWidget(self)
        grid.addWidget(self.slp_search_job_list_widget, 4, 0, 1, 5)

        hbox = QHBoxLayout()
        hbox.addWidget(QLabel(_('GS Data Downloaded') + ':'))
        self.data_label = QLabel('?')
        hbox.addWidget(self.data_label)
        hbox.addStretch(1)
        grid.addLayout(hbox, 5, 0)

        # Blockchain Tab
        grid = QGridLayout(blockchain_tab)
        msg =  ' '.join([
            _("Electron Cash connects to several nodes in order to download block headers and find out the longest blockchain."),
            _("This blockchain is used to verify the transactions sent by your transaction server.")
        ])
        self.status_label = QLabel('')
        self.status_label.setTextInteractionFlags(self.status_label.textInteractionFlags() | Qt.TextSelectableByMouse)
        grid.addWidget(QLabel(_('Status') + ':'), 0, 0)
        grid.addWidget(self.status_label, 0, 1, 1, 3)
        grid.addWidget(HelpButton(msg), 0, 4)

        self.server_label = QLabel('')
        self.server_label.setTextInteractionFlags(self.server_label.textInteractionFlags() | Qt.TextSelectableByMouse)
        msg = _("Electron Cash sends your wallet addresses to a single server, in order to receive your transaction history.")
        grid.addWidget(QLabel(_('Server') + ':'), 1, 0)
        grid.addWidget(self.server_label, 1, 1, 1, 3)
        grid.addWidget(HelpButton(msg), 1, 4)

        self.height_label = QLabel('')
        self.height_label.setTextInteractionFlags(self.height_label.textInteractionFlags() | Qt.TextSelectableByMouse)
        msg = _('This is the height of your local copy of the blockchain.')
        grid.addWidget(QLabel(_('Blockchain') + ':'), 2, 0)
        grid.addWidget(self.height_label, 2, 1)
        grid.addWidget(HelpButton(msg), 2, 4)

        self.split_label = QLabel('')
        self.split_label.setTextInteractionFlags(self.split_label.textInteractionFlags() | Qt.TextSelectableByMouse)
        grid.addWidget(self.split_label, 3, 0, 1, 3)

        self.nodes_list_widget = NodesListWidget(self)
        grid.addWidget(self.nodes_list_widget, 5, 0, 1, 5)

        vbox = QVBoxLayout()
        vbox.addWidget(tabs)
        self.layout_ = vbox

        self.fill_in_proxy_settings()
        self.update()

    def use_slp_gs(self):
        if self.slp_gs_enable_cb.isChecked():
            self.config.set_key('slp_validator_graphsearch_enabled', True)
        else:
            self.config.set_key('slp_validator_graphsearch_enabled', False)
        self.slp_gs_list_widget.update()

    def check_disable_proxy(self, b):
        if not self.config.is_modifiable('proxy'):
            b = False
        for w in [self.proxy_mode, self.proxy_host, self.proxy_port, self.proxy_user, self.proxy_password]:
            w.setEnabled(b)

    def get_set_server_flags(self):
        return (self.config.is_modifiable('server'),
                (not self.autoconnect_cb.isChecked()
                 and not self.preferred_only_cb.isChecked())
               )

    def can_set_server(self, server):
        return bool(self.get_set_server_flags()[0]
                    and not self.network.server_is_blacklisted(server)
                    and (not self.network.is_whitelist_only()
                         or self.network.server_is_whitelisted(server))
                    )

    def enable_set_server(self):
        modifiable, notauto = self.get_set_server_flags()
        if modifiable:
            self.server_host.setEnabled(notauto)
            self.server_port.setEnabled(notauto)
        else:
            for w in [self.autoconnect_cb, self.server_host, self.server_port]:
                w.setEnabled(False)

    def update(self):
        host, port, protocol, proxy_config, auto_connect = self.network.get_parameters()
        preferred_only = self.network.is_whitelist_only()
        if not self.server_host.hasFocus() and not self.server_port.hasFocus():
            self.server_host.setText(host)
            self.server_port.setText(port)
        self.ssl_cb.setChecked(protocol=='s')
        ssl_disable = self.ssl_cb.isChecked() and not self.tor_cb.isChecked() and not host.lower().endswith('.onion')
        for w in [self.ssl_cb]:#, self.ssl_help]:
            w.setDisabled(ssl_disable)
        self.autoconnect_cb.setChecked(auto_connect)
        self.preferred_only_cb.setChecked(preferred_only)

        host = self.network.interface.host if self.network.interface else pgettext('Referencing server', 'None')
        self.server_label.setText(host)

        self.set_protocol(protocol)
        self.servers = self.network.get_servers()
        def protocol_suffix():
            if protocol == 't':
                return '  (non-SSL)'
            elif protocol == 's':
                return '  [SSL]'
            return ''
        server_list_txt = (_('Server peers') if self.network.is_connected() else _('Servers')) + " ({})".format(len(self.servers))
        server_list_txt += protocol_suffix()
        self.server_list_label.setText(server_list_txt)
        if self.network.blacklisted_servers:
            bl_srv_ct_str = ' ({}) <a href="ViewBanList">{}</a>'.format(len(self.network.blacklisted_servers), _("View ban list..."))
        else:
            bl_srv_ct_str = " (0)<i> </i>" # ensure rich text
        servers_whitelisted = set(get_eligible_servers(self.servers, protocol)).intersection(self.network.whitelisted_servers) - self.network.blacklisted_servers
        self.legend_label.setText(ServerFlag.Symbol[ServerFlag.Preferred] + "=" + _("Preferred") + " ({})".format(len(servers_whitelisted)) + "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
                                  + ServerFlag.Symbol[ServerFlag.Banned] + "=" + _("Banned") + bl_srv_ct_str)
        self.servers_list.update(self.network, self.servers, self.protocol, self.tor_cb.isChecked())
        self.enable_set_server()

        height_str = "%d "%(self.network.get_local_height()) + _('blocks')
        self.height_label.setText(height_str)
        n = len(self.network.get_interfaces())
        status = _("Connected to %d nodes.")%n if n else _("Not connected")
        if n: status += protocol_suffix()
        self.status_label.setText(status)
        chains = self.network.get_blockchains()
        if len(chains)>1:
            chain = self.network.blockchain()
            checkpoint = chain.get_base_height()
            name = chain.get_name()
            msg = _('Chain split detected at block %d')%checkpoint + '\n'
            msg += (_('You are following branch') if auto_connect else _('Your server is on branch'))+ ' ' + name
            msg += ' (%d %s)' % (chain.get_branch_size(), _('blocks'))
        else:
            msg = ''
        self.split_label.setText(msg)
        self.nodes_list_widget.update(self.network)
        self.slp_gs_list_widget.update()
        self.slp_gs_server_host.setText(self.network.slp_gs_host)
        self.slp_gs_enable_cb.setChecked(self.config.get('slp_validator_graphsearch_enabled', False))
        self.slp_search_job_list_widget.update()

    def fill_in_proxy_settings(self):
        host, port, protocol, proxy_config, auto_connect = self.network.get_parameters()
        if not proxy_config:
            proxy_config = {"mode": "none", "host": "localhost", "port": "9050"}

        b = proxy_config.get('mode') != "none"
        self.check_disable_proxy(b)
        if b:
            self.proxy_cb.setChecked(True)
            self.proxy_mode.setCurrentIndex(
                self.proxy_mode.findText(str(proxy_config.get("mode").upper())))

        self.proxy_host.setText(proxy_config.get("host"))
        self.proxy_port.setText(proxy_config.get("port"))
        self.proxy_user.setText(proxy_config.get("user", ""))
        self.proxy_password.setText(proxy_config.get("password", ""))

    def layout(self):
        return self.layout_

    def set_protocol(self, protocol):
        if protocol != self.protocol:
            self.protocol = protocol

    def change_protocol(self, use_ssl):
        p = 's' if use_ssl else 't'
        host = self.server_host.text()
        pp = self.servers.get(host, networks.net.DEFAULT_PORTS)
        if p not in pp.keys():
            p = list(pp.keys())[0]
        port = pp[p]
        self.server_host.setText(host)
        self.server_port.setText(port)
        self.set_protocol(p)
        self.set_server()

    def follow_branch(self, index):
        self.network.follow_chain(index)
        self.update()

    def follow_server(self, server):
        self.network.switch_to_interface(server)
        host, port, protocol, proxy, auto_connect = self.network.get_parameters()
        host, port, protocol = deserialize_server(server)
        self.network.set_parameters(host, port, protocol, proxy, auto_connect)
        self.update()

    def server_changed(self, x):
        if x:
            self.change_server(str(x.text(0)), self.protocol)

    def change_server(self, host, protocol):
        pp = self.servers.get(host, networks.net.DEFAULT_PORTS)
        if protocol and protocol not in protocol_letters:
            protocol = None
        if protocol:
            port = pp.get(protocol)
            if port is None:
                protocol = None
        if not protocol:
            if 's' in pp.keys():
                protocol = 's'
                port = pp.get(protocol)
            else:
                protocol = list(pp.keys())[0]
                port = pp.get(protocol)
        self.server_host.setText(host)
        self.server_port.setText(port)
        self.ssl_cb.setChecked(protocol=='s')

    def accept(self):
        pass

    def set_server(self, onion_hack=False):
        host, port, protocol, proxy, auto_connect = self.network.get_parameters()
        host = str(self.server_host.text())
        port = str(self.server_port.text())
        protocol = 's' if self.ssl_cb.isChecked() else 't'
        if onion_hack:
            # Fix #1174 -- bring back from the dead non-SSL support for .onion only in a safe way
            if host.lower().endswith('.onion'):
                self.print_error("Onion/TCP hack: detected .onion, forcing TCP (non-SSL) mode")
                protocol = 't'
                self.ssl_cb.setChecked(False)
        auto_connect = self.autoconnect_cb.isChecked()
        self.network.set_parameters(host, port, protocol, proxy, auto_connect)

    def set_slp_server(self, server=None):
        if not server:
            server = str(self.slp_gs_server_host.text())
        else:
            self.slp_gs_server_host.setText(server)
        self.network.slp_gs_host = server
        self.config.set_key('slp_gs_host', self.network.slp_gs_host)
        self.slp_gs_list_widget.update()

    def set_proxy(self):
        host, port, protocol, proxy, auto_connect = self.network.get_parameters()
        if self.proxy_cb.isChecked():
            proxy = { 'mode':str(self.proxy_mode.currentText()).lower(),
                      'host':str(self.proxy_host.text()),
                      'port':str(self.proxy_port.text()),
                      'user':str(self.proxy_user.text()),
                      'password':str(self.proxy_password.text())}
        else:
            proxy = None
            self.tor_cb.setChecked(False)
        self.network.set_parameters(host, port, protocol, proxy, auto_connect)

    def suggest_proxy(self, found_proxy):
        if not found_proxy:
            self.tor_cb.setEnabled(False)
            self.tor_cb.setChecked(False) # It's not clear to me that if the tor service goes away and comes back later, and in the meantime they unchecked proxy_cb, that this should remain checked. I can see it being confusing for that to be the case. Better to uncheck. It gets auto-re-checked anyway if it comes back and it's the same due to code below. -Calin
            return
        self.tor_proxy = found_proxy
        self.tor_cb.setText("Use Tor proxy at port " + str(found_proxy[1]))
        if (self.proxy_mode.currentIndex() == self.proxy_mode.findText('SOCKS5')
            and self.proxy_host.text() == found_proxy[0]
            and self.proxy_port.text() == str(found_proxy[1])
            and self.proxy_cb.isChecked()):
            self.tor_cb.setChecked(True)
        self.tor_cb.setEnabled(True)

    def use_tor_proxy(self, use_it):
        if not use_it:
            self.proxy_cb.setChecked(False)
        else:
            socks5_mode_index = self.proxy_mode.findText('SOCKS5')
            if socks5_mode_index == -1:
                print_error("[network_dialog] can't find proxy_mode 'SOCKS5'")
                return
            self.proxy_mode.setCurrentIndex(socks5_mode_index)
            self.proxy_host.setText("127.0.0.1")
            self.proxy_port.setText(str(self.tor_proxy[1]))
            self.proxy_user.setText("")
            self.proxy_password.setText("")
            self.tor_cb.setChecked(True)
            self.proxy_cb.setChecked(True)
        self.check_disable_proxy(use_it)
        self.set_proxy()

    def proxy_settings_changed(self):
        self.tor_cb.setChecked(False)

    def set_blacklisted(self, server, bl):
        self.network.server_set_blacklisted(server, bl, True)
        self.set_server() # if the blacklisted server is the active server, this will force a reconnect to another server
        self.update()

    def set_whitelisted(self, server, flag):
        self.network.server_set_whitelisted(server, flag, True)
        self.set_server()
        self.update()

    def set_whitelisted_only(self, b):
        self.network.set_whitelist_only(b)
        self.set_server() # forces us to send a set-server to network.py which recomputes eligible servers, etc
        self.update()

    def on_view_blacklist(self, ignored):
        ''' The 'view ban list...' link leads to a modal dialog box where the
        user has the option to clear the entire blacklist. Build that dialog here. '''
        bl = sorted(self.network.blacklisted_servers)
        parent = self.parent()
        if not bl:
            parent.show_error(_("Server ban list is empty!"))
            return
        d = WindowModalDialog(parent.top_level_window(), _("Banned Servers"))
        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_("Banned Servers") + " ({})".format(len(bl))))
        tree = QTreeWidget()
        tree.setHeaderLabels([_('Host'), _('Port')])
        for s in bl:
            host, port, protocol = deserialize_server(s)
            item = QTreeWidgetItem([host, str(port)])
            item.setFlags(Qt.ItemIsEnabled)
            tree.addTopLevelItem(item)
        tree.setIndentation(3)
        h = tree.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        vbox.addWidget(tree)

        clear_but = QPushButton(_("Clear ban list"))
        weakSelf = Weak.ref(self)
        weakD = Weak.ref(d)
        clear_but.clicked.connect(lambda: weakSelf() and weakSelf().on_clear_blacklist() and weakD().reject())
        vbox.addLayout(Buttons(clear_but, CloseButton(d)))
        d.exec_()

    def on_clear_blacklist(self):
        bl = list(self.network.blacklisted_servers)
        blen = len(bl)
        if self.parent().question(_("Clear all {} servers from the ban list?").format(blen)):
            for i,s in enumerate(bl):
                self.network.server_set_blacklisted(s, False, save=bool(i+1 == blen)) # save on last iter
            self.update()
            return True
        return False


class TorDetector(QThread):
    found_proxy = pyqtSignal(object)

    def start(self):
        self.stopQ = queue.Queue() # create a new stopQ blowing away the old one just in case it has old data in it (this prevents races with stop/start arriving too quickly for the thread)
        super().start()

    def stop(self):
        if self.isRunning():
            self.stopQ.put(None)

    def run(self):
        ports = [9050, 9150] # Probable ports for Tor to listen at
        while True:
            for p in ports:
                if TorDetector.is_tor_port(p):
                    self.found_proxy.emit(("127.0.0.1", p))
                    break
            else:
                self.found_proxy.emit(None) # no proxy found, will hide the Tor checkbox
            try:
                self.stopQ.get(timeout=10.0) # keep trying every 10 seconds
                return # we must have gotten a stop signal if we get here, break out of function, ending thread
            except queue.Empty:
                continue # timeout, keep looping

    @staticmethod
    def is_tor_port(port):
        try:
            s = (socket._socketobject if hasattr(socket, "_socketobject") else socket.socket)(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.1)
            s.connect(("127.0.0.1", port))
            # Tor responds uniquely to HTTP-like requests
            s.send(b"GET\n")
            if b"Tor is not an HTTP Proxy" in s.recv(1024):
                return True
        except socket.error:
            pass
        return False
