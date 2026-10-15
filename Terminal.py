#użyj curl na linuxie, nie innym systemie
import http.server
import socketserver
import subprocess
import json
import urllib.parse

PORT = 8000

# Kod HTML strony z terminalem
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
        .error { color: #ff3333; }
    </style>
</head>
<body>
    <h2>Linux Web Terminal (Wbudowany)</h2>
    <div id="terminal">Wpisz polecenie (np. ls, pwd, uname -a) i naciśnij Enter...<br><br></div>
    <div id="input-line">
        <span class="prompt">linuxontab:\$</span>
        <input type="text" id="cmd" autofocus autocomplete="off">
    </div>

    <script>
        const input = document.getElementById('cmd');
        const terminal = document.getElementById('terminal');

        input.addEventListener('keydown', async function(e) {
            if (e.key === 'Enter') {
                const command = input.value.trim();
                if (!command) return;

                input.value = '';
                terminal.innerHTML += `<span class="prompt">linuxontab:$</span> <span class="cmd-echo">${command}</span>\\n`;
                terminal.scrollTop = terminal.scrollHeight;

                try {
                    const response = await fetch('/run', {
                        method: 'POST',
                        body: command
                    });
                    const text = await response.text();
                    terminal.innerHTML += text + '\\n';
                } catch (err) {
                    terminal.innerHTML += `<span class="error">Błąd połączenia: ${err}</span>\\n`;
                }
                terminal.scrollTop = terminal.scrollHeight;
            }
        });
    </script>
</body>
</html>"""

class TerminalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # Serwowanie głównej strony HTML
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode('utf-8'))
        else:
            self.send_error(404, "Nie znaleziono")

    def do_POST(self):
        # Obsługa wykonywania poleceń systemowych
        if self.path == '/run':
            content_length = int(self.headers['Content-Length'])
            command = self.rfile.read(content_length).decode('utf-8')
            
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            
            try:
                # Uruchomienie komendy w powłoce Linuxa z limitem czasu 10 sekund
                result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
                
                output = result.stdout if result.stdout else ""
                if result.stderr:
                    output += f"\n[BŁĄD]:\n{result.stderr}"
                if not result.stdout and not result.stderr:
                    output = "[Polecenie wykonane pomyślnie, brak wyniku]"
                    
                self.wfile.write(output.encode('utf-8'))
            except Exception as e:
                self.wfile.write(f"[BŁĄD SERWERA]: {str(e)}".encode('utf-8'))
        else:
            self.send_error(404)

    # Wyłączenie logowania każdego zapytania w konsoli, by nie śmiecić w terminalu
    def log_message(self, format, *args):
        return

with socketserver.TCPServer(("127.0.0.1", PORT), TerminalHandler) as httpd:
    print(f"Serwer działa pod adresem: http://127.0.0.1:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nZamykanie serwera.")
