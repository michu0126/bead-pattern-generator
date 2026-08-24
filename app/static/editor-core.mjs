export function summarizeGrid(grid) {
  const rows = grid.length;
  const columns = rows ? grid[0].length : 0;
  const counts = new Map();
  let total = 0;
  grid.forEach(row => row.forEach(code => {
    if (!code) return;
    counts.set(code, (counts.get(code) || 0) + 1);
    total += 1;
  }));
  return { rows, columns, total, empty: rows * columns - total, counts };
}

export function editGridCell(grid, row, column, code) {
  if (row < 0 || column < 0 || row >= grid.length || column >= grid[row].length) {
    throw new RangeError('格子坐标超出图纸范围');
  }
  const oldCode = grid[row][column];
  grid[row][column] = code || null;
  return oldCode;
}

export function locateGridCell({ clientX, clientY, left, top, scaleX, scaleY, margin, cellSize }) {
  return {
    column: Math.floor(((clientX - left) * scaleX - margin) / cellSize),
    row: Math.floor(((clientY - top) * scaleY - margin) / cellSize),
  };
}
