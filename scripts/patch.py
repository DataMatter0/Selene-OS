with open(r'c:\Users\rcole\Github Projects\renderer\index.html', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('  const [view,      setView]      = useState("menu");', '  const [view,      setView]      = useState("menu");\n  const [toasts,    setToasts]    = useState([]);')

target = '''        {/* Footer */}
        <div className="main-footer">
          <FooterNav />
        </div>
      </div>
    </div>
  );
}'''

replacement = '''        {/* Footer */}
        <div className="main-footer">
          <FooterNav />
        </div>
        
        {/* Toasts overlay */}
        <div style={{
          position: "fixed", bottom: 40, right: 20, display: "flex", flexDirection: "column", gap: 10,
          zIndex: 9999, pointerEvents: "none"
        }}>
          {toasts.map(toast => (
            <div key={toast.id} style={{
              background: "rgba(7, 5, 26, 0.95)", borderLeft: "3px solid var(--amber)",
              color: "var(--amber)", padding: "10px 15px", boxShadow: "0 4px 12px rgba(0,0,0,0.5)",
              fontSize: 10, fontFamily: "var(--mono)", maxWidth: 300, animation: "fadeIn 0.3s ease"
            }}>
              {toast.text}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}'''

text = text.replace(target, replacement)

with open(r'c:\Users\rcole\Github Projects\renderer\index.html', 'w', encoding='utf-8') as f:
    f.write(text)

print('Success')
