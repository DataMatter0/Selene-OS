const fs = require('fs');

const html = fs.readFileSync('renderer/index.html', 'utf8');
const scriptMatches = html.matchAll(/<script type="text\/babel">([\s\S]*?)<\/script>/g);
let found = false;

for (const match of scriptMatches) {
  found = true;
  const code = match[1];
  try {
    const babel = require('@babel/core');
    babel.transformSync(code, { presets: ['@babel/preset-react'] });
    console.log('Babel compiled successfully');
  } catch (e) {
    console.log('Babel compilation error:\n', e.message);
  }
}

if (!found) {
  console.log('No babel script tags found.');
}
