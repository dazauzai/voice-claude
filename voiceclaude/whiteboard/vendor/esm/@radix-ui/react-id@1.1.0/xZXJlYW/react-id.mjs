/* esm.sh - @radix-ui/react-id@1.1.0 */
import*as t from"react";import{useLayoutEffect as u}from"../../react-use-layout-effect@1.1.0/xZXJlYW/react-use-layout-effect.mjs";var c=t.useId||(()=>{}),f=0;function n(o){let[a,e]=t.useState(c());return u(()=>{o||e(r=>r??String(f++))},[o]),o||(a?`radix-${a}`:"")}export{n as useId};
//# sourceMappingURL=react-id.mjs.map