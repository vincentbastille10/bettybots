<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8" />
  <title>Prévisualisation du bot</title>
  <style>
    body {
      background:#0d1117;
      color:#fff;
      font-family:Inter,system-ui,sans-serif;
      text-align:center;
      padding:40px;
    }
    iframe {
      border:0;
      border-radius:18px;
      width:420px;
      height:580px;
      box-shadow:0 0 25px rgba(0,0,0,.4);
      margin-top:40px;
    }
    button {
      margin-top:20px;
      padding:10px 20px;
      border:none;
      border-radius:8px;
      cursor:pointer;
    }
  </style>
</head>
<body>
  <h1>Prévisualisation du bot</h1>
  <p>Testez votre bot avant de vous abonner.</p>

  <!-- On affiche ici la vraie fenêtre carrée -->
  <iframe src="/chat?tenant={{ tenant }}"></iframe>

  <div>
    <button onclick="window.location='/dashboard'">Retour à la configuration</button>
    <button style="background:#2563eb;color:white" onclick="window.location='/pay?tenant={{ tenant }}'">S’abonner</button>
  </div>
</body>
</html>
