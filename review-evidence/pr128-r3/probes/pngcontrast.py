import struct, zlib, sys
def load(p):
    data=open(p,'rb').read(); i=8; idat=b''
    while i<len(data):
        n=struct.unpack('>I',data[i:i+4])[0]; t=data[i+4:i+8]; c=data[i+8:i+8+n]; i+=12+n
        if t==b'IHDR': w,h=struct.unpack('>II',c[:8])
        elif t==b'IDAT': idat+=c
    raw=zlib.decompress(idat); bpp=4; stride=w*bpp; rows=[]; prev=bytearray(stride); p=0
    for y in range(h):
        f=raw[p]; line=bytearray(raw[p+1:p+1+stride]); p+=1+stride
        for x in range(stride):
            a=line[x-bpp] if x>=bpp else 0; b=prev[x]; c=prev[x-bpp] if x>=bpp else 0
            if f==1: line[x]=(line[x]+a)&255
            elif f==2: line[x]=(line[x]+b)&255
            elif f==3: line[x]=(line[x]+(a+b)//2)&255
            elif f==4:
                pa,pb,pc=abs(b-c),abs(a-c),abs(a+b-2*c); pr=a if pa<=pb and pa<=pc else (b if pb<=pc else c); line[x]=(line[x]+pr)&255
        rows.append(line); prev=line
    return w,h,rows
def lum(rgb):
    def ch(v):
        v/=255; return v/12.92 if v<=0.03928 else ((v+0.055)/1.055)**2.4
    r,g,b=rgb; return 0.2126*ch(r)+0.7152*ch(g)+0.0722*ch(b)
def contrast(a,b):
    la,lb=sorted([lum(a),lum(b)],reverse=True); return (la+0.05)/(lb+0.05)
for name in sys.argv[1:]:
    w,h,rows=load(name)
    px=lambda x,y: tuple(rows[y][x*4:x*4+3])
    # the pending count: the most orange pixels (red well above blue) in the right fifth
    amber=[(x,y) for y in range(h) for x in range(int(w*0.8),w) if px(x,y)[0]-px(x,y)[2]>60]
    xs=[x for x,_ in amber]; ys=[y for _,y in amber]
    glyph=max((px(x,y) for x,y in amber), key=lambda c: abs(c[0]-c[2]))
    bg=px(min(w-1,max(xs)+12),(min(ys)+max(ys))//2)
    print(name.split('/')[-2], name.split('/')[-1], "count box x", min(xs), max(xs), "y", min(ys), max(ys), "bg", bg, "glyph", glyph, "contrast %.2f:1" % contrast(bg,glyph))

def title_contrast(name):
    w,h,rows=load(name)
    px=lambda x,y: tuple(rows[y][x*4:x*4+3])
    # title glyphs: x 60-260 px, the selected row's band (y 395-440 at 2x); bg at x=300
    bg=px(300,402)
    cand=[px(x,y) for y in range(395,440) for x in range(60,260)]
    glyph=max(cand,key=lambda c: sum(abs(c[i]-bg[i]) for i in range(3)))
    return bg, glyph, contrast(bg,glyph)
