const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 3000;
const PUBLIC = path.join(__dirname, 'public');

// Simulated prices (random walk). Swap for a real data feed as needed.
const symbols = { VDHG: 62.4, AAPL: 230.1, BTC: 98000, ETH: 3400, SPY: 590.2 };

function tick() {
  for (const s in symbols) {
    symbols[s] = +(symbols[s] * (1 + (Math.random() - 0.5) * 0.004)).toFixed(2);
  }
}
setInterval(tick, 2000);

http.createServer((req, res) => {
  if (req.url === '/api/prices') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify(symbols));
  }
  const file = path.join(PUBLIC, req.url === '/' ? 'index.html' : path.normalize(req.url).replace(/^(\.\.[/\\])+/, ''));
  if (!file.startsWith(PUBLIC)) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); return res.end('Not found'); }
    const type = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript' }[path.extname(file)] || 'text/plain';
    res.writeHead(200, { 'Content-Type': type });
    res.end(data);
  });
}).listen(PORT, () => console.log(`vdhg-ticker running on http://localhost:${PORT}`));
