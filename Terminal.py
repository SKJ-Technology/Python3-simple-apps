import http.server
import socketserver
import os
import sys
import platform
import shutil

PORT = 8000

HTML_CONTENT = """<!DOCTYPE html>
<html lang="pl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Linux Terminal Web</title>
    <style>
        body { background-color: #1a1a1a; color: #00ff66; font-family: monospace; padding: 20px; margin: 0; }
        #terminal { background-color: #000; border: 1px solid #333; padding: 15px; height: 500px; overflow-y: auto; white-space: pre-wrap; box-shadow: 0 0 15px rgba(0,255,102,0.1); margin-bottom: 10px; }
        #input-line { display: flex; align-items: center; }
        .prompt { font-weight: bold; margin-right: 10px; color: #00ffaa; }
        input { flex-grow: 1; background: transparent; border: none; color: #00ff66; font-family: inherit; font-size: 16px; outline: none; }
        .cmd-echo { color: #ffffff; font-weight: bold; }
    </style>
</head>
<body>
    <h2>Linux Web Terminal (Emulowany w Pythonie)</h2>
    <div id="terminal">Środowisko nie pozwala na subprocess (Errno 38).<br>Uruchomiono bezpieczny emulator terminala w Pythonie.<br>Dostępne komendy: ls, pwd, cd [sciezka], cat [plik], uname, whoami, help, clear<br><br></div>
    <div id="input-line">
        <span class="prompt">linuxontab:\$</span>
        <input type="text" id="cmd" autofocus autocomplete="off">
    </div>

    <script>
        const input = document.getElementById('cmd');
        const terminal = document.getElementById('terminal');
        let history = [];
        let historyIndex = -1;

        input.addEventListener('keydown', async function(e) {
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (history.length > 0 && historyIndex < history.length - 1) {
                    historyIndex++;
                    input.value = history[history.length - 1 - historyIndex];
                }
            }
            else if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (historyIndex > 0) {
                    historyIndex--;
                    input.value = history[history.length - 1 - historyIndex];
                } else if (historyIndex === 0) {
                    historyIndex = -1;
                    input.value = '';
                }
            }
            else if (e.key === 'Enter') {
                const command = input.value.trim();
                if (!command) return;

                history.push(command);
                historyIndex = -1;

                input.value = '';
                terminal.innerHTML += `<span class="prompt">linuxontab:$</span> <span class="cmd-echo">${command}</span>\\n`;
                
                if (command.toLowerCase() === 'clear') {
                    terminal.innerHTML = '';
                    return;
                }

                terminal.scrollTop = terminal.scrollHeight;

                try {
                    const response = await fetch('/run', {
                        method: 'POST',
                        body: command
                    });
                    const text = await response.text();
                    terminal.innerHTML += text + '\\n';
                } catch (err) {
                    terminal.innerHTML += `<span style="color:red">Błąd połączenia: ${err}</span>\\n`;
                }
                terminal.scrollTop = terminal.scrollHeight;
            }
        });
    </script>
</body>
</html>"""

class TerminalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode('utf-8'))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/run':
            content_length = int(self.headers['Content-Length'])
            full_command = self.rfile.read(content_length).decode('utf-8').strip()
            
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            
            # Podział na komendę i argumenty
            parts = full_command.split()
            cmd = parts[0] if parts else ""
            args = parts[1:] if len(parts) > 1 else []
            
            output = ""
            
            try:
                # Emulator komendy: ls
                if cmd == "ls":
                    target_dir = args[0] if args else "."
                    files = os.listdir(target_dir)
                    output = "\n".join(files) if files else "[Katalog jest pusty]"
                
                # Emulator komendy: pwd
                elif cmd == "pwd":
                    output = os.getcwd()
                
                # Emulator komendy: cd
                elif cmd == "cd":
                    if args:
                        os.chdir(args[0])
                        output = f"Zmieniono katalog na: {os.getcwd()}"
                    else:
                        output = "Podaj ścieżkę, np: cd /tmp"
                
                # Emulator komendy: cat
                elif cmd == "cat":
                    if args:
                        if os.path.exists(args[0]) and os.path.isfile(args[0]):
                            with open(args[0], 'r', encoding='utf-8', errors='ignore') as f:
                                output = f.read()
                        else:
                            output = f"cat: {args[0]}: Brak takiego pliku"
                    else:
                        output = "Użycie: cat [nazwa_pliku]"
                
                # Emulator komendy: uname
                elif cmd == "uname":
                    output = f"{platform.system()} {platform.release()} {platform.machine()}"
                
                # Emulator komendy: whoami
                elif cmd == "whoami":
                    # Próba pobrania loginu, jeśli zablokowane - alternatywa
                    try:
                        output = os.getlogin()
                    except:
                        output = f"UID: {os.getuid() if hasattr(os, 'getuid') else 'unknown'}"
                
                # Emulator komendy: help
                elif cmd == "help":
                    output = "Dostępne komendy Py-Terminala:\n"
                    output += "  ls          - lista plików w bieżącym katalogu\n"
                    output += "  pwd         - ścieżka bieżącego katalogu\n"
                    output += "  cd [kat]    - zmiana katalogu\n"
                    output += "  cat [plik]  - wyświetlenie zawartości pliku\n"
                    output += "  uname       - informacje o systemie\n"
                    output += "  whoami      - nazwa użytkownika\n"
                    output += "  clear       - czyszczenie ekranu"
                
                else:
                    output = f"Komenda '{cmd}' nie jest obsługiwana w tym środowisku chmurowym (blokada Errno 38)."
                    
                if not output:
                    output = "[Brak wyniku]"
                    
                self.wfile.write(output.encode('utf-8'))
                
            except Exception as e:
                self.wfile.write(f"[BŁĄD EMULATORA]: {str(e)}".encode('utf-8'))
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        return

with socketserver.TCPServer(("0.0.0.0", PORT), TerminalHandler) as httpd:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
