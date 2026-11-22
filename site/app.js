import { Map, Popup, setWorkerUrl } from './vendor/maplibre-gl.mjs';

setWorkerUrl(new URL('./vendor/maplibre-gl-worker.mjs', import.meta.url).href);

const VIEWS = { a: 'Alle Netze', t: 'Telekom', e: '1&1', g: 'TNG', p: 'Telekom-Polygone' };
const PROVIDER_OF_VIEW = { a: null, t: 'telekom', e: 'einsundeins', g: 'tng', p: null };
const FRANKFURT = [[8.40, 49.98], [8.82, 50.26]];

const hash = new URLSearchParams(location.hash.slice(1));
let view = VIEWS[hash.get('v')] ? hash.get('v') : 'a';
const cam = (hash.get('c') || '').split(',').map(Number);
const hasCam = cam.length === 3 && cam.every(Number.isFinite);

const map = new Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/positron',
  center: hasCam ? [cam[0], cam[1]] : [8.682, 50.111],
  zoom: hasCam ? cam[2] : 11.5,
  maxBounds: FRANKFURT,
  hash: false,
});

let cells, districts, summary, polygons;

function flatten(data) {
  data.features.forEach((f, i) => {
    f.id = i;
    const p = f.properties;
    if (p.s) { p.y = -1; p.u = 100; p.nn = 0; return; }
    const v = p[view] || [0, 0, 100];
    p.y = v[0]; p.nn = v[1]; p.u = v[2];
  });
  return data;
}

function writeHash(key, value) {
  const p = new URLSearchParams(location.hash.slice(1));
  p.set(key, value);
  history.replaceState(null, '', '#' + p.toString());
}

function paint() {
  const showPolygons = view === 'p';
  map.setLayoutProperty('cells-fill', 'visibility', showPolygons ? 'none' : 'visible');
  map.setLayoutProperty('districts-line', 'visibility', showPolygons ? 'none' : 'visible');
  map.setLayoutProperty('polygons-fill', 'visibility', showPolygons ? 'visible' : 'none');
  if (!showPolygons && cells) map.getSource('cells').setData(flatten(cells));
  renderDistricts();
  renderStats();
  writeHash('v', view);
}

function bboxOf(geom) {
  let minX = 180, minY = 90, maxX = -180, maxY = -90;
  const walk = (c) => {
    if (typeof c[0] === 'number') {
      minX = Math.min(minX, c[0]); maxX = Math.max(maxX, c[0]);
      minY = Math.min(minY, c[1]); maxY = Math.max(maxY, c[1]);
    } else c.forEach(walk);
  };
  walk(geom.coordinates);
  return [[minX, minY], [maxX, maxY]];
}

function renderDistricts() {
  if (view === 'p' || !districts) {
    document.getElementById('districts').replaceChildren();
    return;
  }
  const rows = [...districts.features].sort(
    (a, b) => b.properties[view][0] - a.properties[view][0] || a.properties.name.localeCompare(b.properties.name),
  );
  document.getElementById('districts').replaceChildren(...rows.map((f) => {
    const li = document.createElement('li');
    li.tabIndex = 0;
    const name = document.createElement('span'); name.textContent = f.properties.name;
    const val = document.createElement('span'); val.textContent = `${f.properties[view][0]} %`;
    li.append(name, val);
    const go = () => map.fitBounds(bboxOf(f.geometry), { padding: 40, maxZoom: 14.5 });
    li.addEventListener('click', go);
    li.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    return li;
  }));
}

function renderStats() {
  document.getElementById('phase').textContent = '';
  if (view === 'p') {
    const date = polygons && polygons.version ? polygons.version.slice(0, 10) : '';
    document.getElementById('stats').textContent =
      `Flächen, in denen die Telekom Glasfaser ausgebaut hat${date ? ' (Stand ' + date + ')' : ''}. ` +
      'Ein Hinweis auf den technischen Ausbau, keine Bestellzusage und keine Messung an Ihrer Adresse.';
    return;
  }
  if (!summary) return;
  const p = summary.providers[PROVIDER_OF_VIEW[view]] || null;
  const parts = [];
  if (p) {
    parts.push(`Gemessen: ${p.measured.toLocaleString('de-DE')} Adressen`);
    if (p.error_rate !== null) {
      const [lo, hi] = p.error_ci.map((x) => (x * 100).toFixed(1));
      parts.push(`Fehlerquote auf Stichprobe: ${(p.error_rate * 100).toFixed(1)} % (95 %: ${lo}–${hi} %)`);
    }
    if (p.last_measured) parts.push(`Stand: ${p.last_measured.slice(0, 10)}`);
    if (p.pending) document.getElementById('phase').textContent = `${p.pending.toLocaleString('de-DE')} Messungen ausstehend`;
  } else {
    parts.push(`Adressen in Frankfurt: ${summary.addresses.toLocaleString('de-DE')}`);
  }
  document.getElementById('stats').textContent = parts.join(' · ');
}

function popupHtml(p) {
  if (p.s) return `<b>zu wenige Adressen</b><br>${p.n} Adressen in diesem 100-m-Feld; hier wird kein Ergebnis gezeigt.`;
  const line = (k) => `${VIEWS[k]}: ca. ${p[k][0]} % bestellbar, ca. ${p[k][1]} % nicht, ca. ${p[k][2]} % unbekannt`;
  const order = ['a', 't', 'e', 'g'];
  return `<b>${p.n} Adressen in diesem 100-m-Feld</b><br>${order.map(line).join('<br>')}`;
}

map.on('load', async () => {
  const names = ['cells.geojson', 'districts.geojson', 'summary.json', 'polygons.geojson'];
  const vals = await Promise.all(names.map((n) => fetch(`./data/${n}`).then((r) => (r.ok ? r.json() : null))));
  [cells, districts, summary, polygons] = vals;
  if (!polygons) polygons = { type: 'FeatureCollection', features: [] };
  map.addSource('cells', { type: 'geojson', data: flatten(cells) });
  map.addSource('districts', { type: 'geojson', data: districts });
  map.addSource('polygons', { type: 'geojson', data: polygons });
  map.addLayer({
    id: 'cells-fill', type: 'fill', source: 'cells',
    paint: {
      'fill-color': ['case',
        ['==', ['get', 'y'], -1], '#eeeeee',
        ['>=', ['get', 'u'], 60], '#cccccc',
        ['step', ['get', 'y'], '#f1eef6', 10, '#bdc9e1', 40, '#74a9cf', 70, '#2b8cbe', 90, '#045a8d']],
      'fill-opacity': ['case', ['==', ['get', 'y'], -1], 0.5, ['>=', ['get', 'u'], 60], 0.35, 0.75],
    },
  });
  map.addLayer({ id: 'districts-line', type: 'line', source: 'districts', paint: { 'line-color': '#555', 'line-width': 1 } });
  map.addLayer({ id: 'polygons-fill', type: 'fill', source: 'polygons', layout: { visibility: 'none' },
                 paint: { 'fill-color': '#66bd6d', 'fill-opacity': 0.25 } });
  map.on('click', 'cells-fill', (e) => {
    const f = cells.features[e.features[0].id];
    new Popup().setLngLat(e.lngLat).setHTML(popupHtml(f.properties)).addTo(map);
  });
  map.on('mouseenter', 'cells-fill', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'cells-fill', () => { map.getCanvas().style.cursor = ''; });
  paint();
});

for (const input of document.querySelectorAll('input[name=view]')) {
  input.checked = input.value === view;
  input.addEventListener('change', () => { view = input.value; if (cells) paint(); });
}

map.on('moveend', () => {
  const c = map.getCenter();
  writeHash('c', `${c.lng.toFixed(3)},${c.lat.toFixed(3)},${map.getZoom().toFixed(1)}`);
});
