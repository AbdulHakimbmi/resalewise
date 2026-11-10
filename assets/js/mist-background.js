(function() {
    const c = document.createElement('canvas');
    c.className = 'mist-canvas';
    c.setAttribute('aria-hidden', 'true');
    document.body.prepend(c);
    const g = c.getContext('webgl', { alpha: false, antialias: false });
    if (!g) { c.remove(); return }
    const v = `attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}`;
    // Palette re-themed to bone/graphite + navy: neutral dark base, cool
    // slate mid-tone, muted navy highlight, subtle navy cursor glow.
    const f = `precision highp float;uniform float t;uniform vec2 r,m;float h(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}float n(vec2 p){vec2 i=floor(p),q=fract(p);float a=h(i),b=h(i+vec2(1.,0.)),c=h(i+vec2(0.,1.)),d=h(i+vec2(1.,1.));vec2 u=q*q*(3.-2.*q);return mix(a,b,u.x)+(c-a)*u.y*(1.-u.x)+(d-b)*u.x*u.y;}float fb(vec2 p){float x=0.,a=.5;for(int i=0;i<6;i++){x+=a*n(p);p*=2.;a*=.5;}return x;}void main(){vec2 u=gl_FragCoord.xy/r;u.x*=r.x/r.y;vec2 z=m/r;z.x*=r.x/r.y;float d=distance(u,z);vec2 q=vec2(fb(u+.07*t),fb(u+vec2(1.)));vec2 x=vec2(fb(u+q+vec2(1.7,9.2)+.15*t),fb(u+q+vec2(8.3,2.8)+.126*t));float k=fb(u+x);vec3 a=vec3(.028,.030,.036),b=vec3(.075,.090,.130),e=vec3(.19,.24,.38);vec3 col=mix(a,b,k);col=mix(col,e,dot(q,x)*.42);col+=smoothstep(.38,0.,d)*.035*vec3(.55,.68,1.0);col=pow(col,vec3(1.06))*1.20;gl_FragColor=vec4(col,1.);}`;

    function s(t, x) {
        const q = g.createShader(t);
        g.shaderSource(q, x);
        g.compileShader(q);
        return q
    }
    const p = g.createProgram();
    g.attachShader(p, s(g.VERTEX_SHADER, v));
    g.attachShader(p, s(g.FRAGMENT_SHADER, f));
    g.linkProgram(p);
    if (!g.getProgramParameter(p, g.LINK_STATUS)) { c.remove(); return }
    g.useProgram(p);
    const b = g.createBuffer();
    g.bindBuffer(g.ARRAY_BUFFER, b);
    g.bufferData(g.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), g.STATIC_DRAW);
    const a = g.getAttribLocation(p, 'p');
    g.enableVertexAttribArray(a);
    g.vertexAttribPointer(a, 2, g.FLOAT, false, 0, 0);
    const tl = g.getUniformLocation(p, 't'),
        rl = g.getUniformLocation(p, 'r'),
        ml = g.getUniformLocation(p, 'm');
    let mouse = { x: innerWidth * .55, y: innerHeight * .55 };

    function resize() {
        let d = Math.min(devicePixelRatio || 1, 1.5);
        c.width = innerWidth * d;
        c.height = innerHeight * d;
        g.viewport(0, 0, c.width, c.height)
    }
    addEventListener('resize', resize, { passive: true });
    addEventListener('pointermove', e => {
        mouse.x = e.clientX;
        mouse.y = innerHeight - e.clientY
    }, { passive: true });
    resize();

    function draw(x) {
        g.uniform1f(tl, x * .001);
        g.uniform2f(rl, c.width, c.height);
        g.uniform2f(ml, mouse.x * (c.width / innerWidth), mouse.y * (c.height / innerHeight));
        g.drawArrays(g.TRIANGLES, 0, 6);
        requestAnimationFrame(draw)
    }
    requestAnimationFrame(draw)
})();