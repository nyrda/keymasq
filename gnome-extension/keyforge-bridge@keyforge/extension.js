import GLib from 'gi://GLib'
import Gio from 'gi://Gio'
import Meta from 'gi://Meta'
import Shell from 'gi://Shell'

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js'

class KeyforgeBridge {
    static PROTOCOL_VERSION = 2

    constructor() {
        this._socketPath = GLib.build_filenamev([
            GLib.get_user_runtime_dir(),
            'keyforge',
            'gnome-bridge.sock',
        ])
        this._client = null
        this._connection = null
        this._in = null
        this._out = null
        this._readCancellable = null
        this._connected = false
        this._focusSignal = 0
        this._titleSignal = 0
        this._currentFocusWindow = null
        this._reconnectSource = 0
    }

    enable() {
        if (this._focusSignal === 0)
            this._focusSignal = global.display.connect('notify::focus-window', () => {
                this._trackFocusedWindow()
                this._sendFocusChanged()
            })

        this._trackFocusedWindow()
        this._connect()
    }

    disable() {
        this._untrackFocusedWindow()

        if (this._focusSignal !== 0) {
            global.display.disconnect(this._focusSignal)
            this._focusSignal = 0
        }

        if (this._reconnectSource !== 0) {
            GLib.Source.remove(this._reconnectSource)
            this._reconnectSource = 0
        }

        this._disconnect()
    }

    _trackFocusedWindow() {
        this._untrackFocusedWindow()
        this._currentFocusWindow = global.display.focus_window
        if (this._currentFocusWindow) {
            this._titleSignal = this._currentFocusWindow.connect('notify::title', () => {
                this._sendFocusChanged()
            })
        }
    }

    _untrackFocusedWindow() {
        if (this._currentFocusWindow && this._titleSignal !== 0) {
            this._currentFocusWindow.disconnect(this._titleSignal)
            this._titleSignal = 0
        }
        this._currentFocusWindow = null
    }

    _connect() {
        this._disconnect()

        this._client = new Gio.SocketClient()
        const address = Gio.UnixSocketAddress.new(this._socketPath)
        this._client.connect_async(address, null, (_client, res) => {
            try {
                this._connection = this._client.connect_finish(res)
                this._in = new Gio.DataInputStream({
                    base_stream: this._connection.get_input_stream(),
                })
                this._out = this._connection.get_output_stream()
                this._readCancellable = new Gio.Cancellable()
                this._connected = true

                this._sendMessage({type: 'hello', protocol: KeyforgeBridge.PROTOCOL_VERSION})
                this._sendFocusChanged()
                this._readLoop()
            } catch (_e) {
                this._scheduleReconnect()
            }
        })
    }

    _disconnect() {
        this._connected = false

        if (this._readCancellable) {
            this._readCancellable.cancel()
            this._readCancellable = null
        }

        if (this._in) {
            try {
                this._in.close(null)
            } catch (_e) {
            }
            this._in = null
        }

        if (this._out) {
            try {
                this._out.close(null)
            } catch (_e) {
            }
            this._out = null
        }

        if (this._connection) {
            try {
                this._connection.close(null)
            } catch (_e) {
            }
            this._connection = null
        }

        this._client = null
    }

    _scheduleReconnect() {
        if (this._reconnectSource !== 0)
            return

        this._reconnectSource = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => {
            this._reconnectSource = 0
            this._connect()
            return GLib.SOURCE_REMOVE
        })
    }

    _readLoop() {
        if (!this._in || !this._readCancellable)
            return

        this._in.read_line_async(GLib.PRIORITY_DEFAULT, this._readCancellable, (stream, res) => {
            try {
                const [line] = stream.read_line_finish_utf8(res)
                if (!line) {
                    this._disconnect()
                    this._scheduleReconnect()
                    return
                }

                let message = null
                try {
                    message = JSON.parse(line)
                } catch (_e) {
                    message = null
                }

                if (message)
                    this._handleMessage(message)

                this._readLoop()
            } catch (_e) {
                this._disconnect()
                this._scheduleReconnect()
            }
        })
    }

    _handleMessage(message) {
        if (message.type === 'get_pointer') {
            const [x, y, mods] = global.get_pointer()
            this._sendMessage({
                type: 'pointer',
                request_id: Number(message.request_id || 0),
                x,
                y,
                mods,
            })
        } else if (message.type === 'get_active_window') {
            const requestId = Number(message.request_id || 0)
            this._sendMessage({
                type: 'active_window',
                request_id: requestId,
                ...this._activeWindowPayload(),
            })
        } else if (message.type === 'activate_title') {
            const title = String(message.title || '')
            const requestId = Number(message.request_id || 0)
            const wins = global.get_window_actors().map(a => a.meta_window)
            const target = wins.find(w => w.get_title() === title)
            if (target) {
                target.activate(global.get_current_time())
                this._sendMessage({
                    type: 'activated',
                    request_id: requestId,
                    title,
                    found: true,
                    ...this._activeWindowPayload(target),
                })
            } else {
                const titles = wins.map(w => w.get_title())
                this._sendMessage({type: 'activated', request_id: requestId, title, found: false, available_titles: titles})
            }
        } else if (message.type === 'dispatch') {
            this._handleDispatch(message)
        }
    }

    _handleDispatch(message) {
        const requestId = Number(message.request_id || 0)
        const dispatcher = String(message.dispatcher || '').trim()
        const args = String(message.args || '').trim()

        if (dispatcher === 'workspace') {
            this._handleWorkspaceDispatch(requestId, args)
            return
        }

        if (dispatcher === 'move_to_workspace') {
            this._handleMoveToWorkspaceDispatch(requestId, args)
            return
        }

        if (dispatcher === 'close_active') {
            this._handleCloseActive(requestId)
            return
        }

        if (dispatcher === 'fullscreen') {
            this._handleFullscreen(requestId, args)
            return
        }

        if (dispatcher === 'maximize') {
            this._handleMaximize(requestId, args)
            return
        }

        this._dispatchResult(requestId, false, `unsupported GNOME dispatcher: ${dispatcher}`)
    }

    _dispatchResult(requestId, ok, message, win = null) {
        this._sendMessage({
            type: 'dispatch_result',
            request_id: requestId,
            ok,
            message,
            ...this._activeWindowPayload(win),
        })
    }

    _getWorkspaceManager() {
        return global.workspace_manager || global.get_workspace_manager?.() || null
    }

    _workspaceCount(manager) {
        if (!manager)
            return 0
        if (typeof manager.n_workspaces === 'number')
            return manager.n_workspaces
        if (typeof manager.get_n_workspaces === 'function')
            return Number(manager.get_n_workspaces())
        return 0
    }

    _resolveWorkspace(manager, args) {
        if (!manager)
            return {ok: false, message: 'workspace manager unavailable'}

        const activeWorkspace = manager.get_active_workspace?.() || null
        const workspaceCount = this._workspaceCount(manager)
        let targetIndex = -1

        if (args === 'next' || args === 'prev') {
            if (!activeWorkspace)
                return {ok: false, message: 'active workspace unavailable'}
            const activeIndex = Number(activeWorkspace.index())
            targetIndex = args === 'next' ? activeIndex + 1 : activeIndex - 1
        } else {
            const parsedIndex = Number.parseInt(args, 10)
            if (!Number.isInteger(parsedIndex) || parsedIndex < 1)
                return {ok: false, message: 'workspace expects next, prev, or a workspace number'}
            targetIndex = parsedIndex - 1
        }

        if (targetIndex < 0 || targetIndex >= workspaceCount)
            return {ok: false, message: `workspace ${targetIndex + 1} does not exist`}

        const workspace = manager.get_workspace_by_index?.(targetIndex) || null
        if (!workspace)
            return {ok: false, message: `workspace ${targetIndex + 1} does not exist`}

        return {
            ok: true,
            workspace,
            index: targetIndex,
        }
    }

    _handleWorkspaceDispatch(requestId, args) {
        const manager = this._getWorkspaceManager()
        const resolved = this._resolveWorkspace(manager, args)
        if (!resolved.ok) {
            this._dispatchResult(requestId, false, resolved.message)
            return
        }

        const timestamp = global.get_current_time()
        const focusedWindow = global.display.focus_window
        if (focusedWindow && focusedWindow.located_on_workspace?.(resolved.workspace))
            resolved.workspace.activate_with_focus(focusedWindow, timestamp)
        else
            resolved.workspace.activate(timestamp)
        this._dispatchResult(requestId, true, `switched to workspace ${resolved.index + 1}`)
    }

    _handleMoveToWorkspaceDispatch(requestId, args) {
        const target = global.display.focus_window
        if (!target) {
            this._dispatchResult(requestId, false, 'no focused window')
            return
        }

        const manager = this._getWorkspaceManager()
        const resolved = this._resolveWorkspace(manager, args)
        if (!resolved.ok) {
            this._dispatchResult(requestId, false, resolved.message)
            return
        }

        target.change_workspace_by_index(resolved.index, false)
        resolved.workspace.activate_with_focus(target, global.get_current_time())
        this._dispatchResult(
            requestId,
            true,
            `moved window to workspace ${resolved.index + 1}`,
            target)
    }

    _handleCloseActive(requestId) {
        const target = global.display.focus_window
        if (!target) {
            this._dispatchResult(requestId, false, 'no focused window')
            return
        }
        target.delete(global.get_current_time())
        this._dispatchResult(requestId, true, 'closed focused window')
    }

    _handleFullscreen(requestId, args) {
        const target = global.display.focus_window
        if (!target) {
            this._dispatchResult(requestId, false, 'no focused window')
            return
        }

        if (args === 'toggle') {
            if (target.is_fullscreen())
                target.unmake_fullscreen()
            else
                target.make_fullscreen()
        } else if (args === 'on') {
            target.make_fullscreen()
        } else if (args === 'off') {
            target.unmake_fullscreen()
        } else {
            this._dispatchResult(requestId, false, 'fullscreen expects toggle, on, or off')
            return
        }

        this._dispatchResult(requestId, true, `fullscreen ${args}`, target)
    }

    _handleMaximize(requestId, args) {
        const target = global.display.focus_window
        if (!target) {
            this._dispatchResult(requestId, false, 'no focused window')
            return
        }

        if (!target.can_maximize?.()) {
            this._dispatchResult(requestId, false, 'focused window cannot be maximized')
            return
        }

        const flags = Meta.MaximizeFlags.BOTH
        if (args === 'toggle') {
            if (target.is_maximized())
                target.unmaximize(flags)
            else
                target.maximize(flags)
        } else if (args === 'on') {
            target.maximize(flags)
        } else if (args === 'off') {
            target.unmaximize(flags)
        } else {
            this._dispatchResult(requestId, false, 'maximize expects toggle, on, or off')
            return
        }

        this._dispatchResult(requestId, true, `maximize ${args}`, target)
    }

    _sendFocusChanged() {
        const payload = this._activeWindowPayload()
        this._sendMessage({type: 'focus_changed', ...payload})
    }

    _activeWindowPayload(win = null) {
        const target = win || global.display.focus_window
        if (!target) {
            return {
                app_id: '',
                wm_class: '',
                title: '',
            }
        }

        const tracker = Shell.WindowTracker.get_default()
        const app = tracker ? tracker.get_window_app(target) : null
        const appId = app ? app.get_id() : ''
        const wmClass = target.get_wm_class ? (target.get_wm_class() || '') : ''
        const title = target.get_title ? (target.get_title() || '') : ''

        return {
            app_id: appId,
            wm_class: wmClass,
            title,
        }
    }

    _sendMessage(message) {
        if (!this._connected || !this._out)
            return

        try {
            const data = `${JSON.stringify(message)}\n`
            const encoded = new TextEncoder().encode(data)
            this._out.write_all(encoded, null)
        } catch (_e) {
            this._disconnect()
            this._scheduleReconnect()
        }
    }
}

export default class KeyforgeBridgeExtension extends Extension {
    enable() {
        this._bridge = new KeyforgeBridge()
        this._bridge.enable()
    }

    disable() {
        if (this._bridge) {
            this._bridge.disable()
            this._bridge = null
        }
    }
}
