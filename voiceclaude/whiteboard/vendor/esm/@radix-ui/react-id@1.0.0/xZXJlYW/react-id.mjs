/* esm.sh - @radix-ui/react-id@1.0.0 */
import*as t from"react";import{useLayoutEffect as f}from"../../react-use-layout-effect@1.0.0/xZXJlYW/react-use-layout-effect.mjs";var u=t.useId||(()=>{}),r=0;function n($){let[e,o]=t.useState(u());return f(()=>{$||o(a=>a??String(r++))},[$]),$||(e?`radix-${e}`:"")}export{n as useId};
//# sourceMappingURL=react-id.mjs.map