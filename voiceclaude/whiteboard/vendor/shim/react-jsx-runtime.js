// react/jsx-runtime over the UMD React global: jsx / jsxs call createElement with key folded into props
const R = window.React;
export const Fragment = R.Fragment;
function make(type, props, key) {
  const { children, ...rest } = props || {};
  if (key !== undefined) rest.key = key;
  return children === undefined ? R.createElement(type, rest) : Array.isArray(children)
    ? R.createElement(type, rest, ...children) : R.createElement(type, rest, children);
}
export const jsx = make;
export const jsxs = make;
export default { Fragment, jsx, jsxs };
