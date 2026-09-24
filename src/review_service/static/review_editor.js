import {EditorState, StateField} from '@codemirror/state';
import {EditorView, Decoration, lineNumbers, highlightActiveLine, drawSelection} from '@codemirror/view';
import {MergeView, unifiedMergeView, getChunks} from '@codemirror/merge';

function stagedDecoration(lines) {
  const numbers = new Set(lines);
  return StateField.define({
    create(state) {
      return Decoration.set([...numbers].filter(n => n >= 1 && n <= state.doc.lines).sort((a, b) => a - b)
        .map(n => Decoration.line({class: 'cm-review-staged-line'}).range(state.doc.line(n).from)));
    },
    update(value, transaction) { return value.map(transaction.changes); },
    provide: field => EditorView.decorations.from(field),
  });
}

export function createDiffEditor(parent, {before, after, editable, nonce, compact, stagedLines, onChange, onSelection}) {
  const listener = side => EditorView.updateListener.of(update => {
    if (update.docChanged && side === 'b') onChange(update.state.doc.toString());
    if (update.selectionSet) {
      const range = update.state.selection.main;
      if (range.empty) return;
      const start = update.state.doc.lineAt(range.from).number;
      const end = update.state.doc.lineAt(Math.max(range.from, range.to - 1)).number;
      onSelection({side, start, end, text: update.state.sliceDoc(range.from, range.to),
                   unsaved: side === 'b' && update.view.state.doc.toString() !== after});
    }
  });
  const extensions = side => [lineNumbers(), highlightActiveLine(), drawSelection(),
    EditorView.cspNonce.of(nonce), EditorState.readOnly.of(side === 'a' || !editable),
    EditorView.editable.of(side === 'b' && editable), listener(side),
    stagedLines ? stagedDecoration(stagedLines[side] || []) : []];
  let view, left, right;
  if (compact) {
    right = new EditorView({parent, state: EditorState.create({doc: after, extensions: [
      ...extensions('b'), unifiedMergeView({original: before, mergeControls: editable, collapseUnchanged: {margin: 3, minSize: 8}})
    ]})});
    view = right;
  } else {
    view = new MergeView({parent, a: {doc: before, extensions: extensions('a')},
      b: {doc: after, extensions: extensions('b')},
      revertControls: editable ? 'a-to-b' : undefined,
      collapseUnchanged: {margin: 3, minSize: 8}});
    left = view.a; right = view.b;
  }
  return {
    get content() { return right.state.doc.toString(); },
    get chunks() { return compact ? getChunks(right.state)?.chunks || [] : view.chunks; },
    get right() { return right; },
    get left() { return left; },
    requestMeasure() { right.requestMeasure(); left?.requestMeasure(); },
    destroy() { view.destroy(); },
  };
}
