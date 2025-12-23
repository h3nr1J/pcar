//Lee variables GET de URL de una página y devuelve como una matriz asociativa.
function getUrlVars()
{
    let vars = [];
    vars[0] = "";
    
	
    return vars;
}

//$("#InicioLibre").ready(function() {
    //var strUrl = getUrlVars()["uid"];  
    //var strMasConsultas = getUrlVars()["mc"]; 
    //$("#ctl00_cplPrincipal_txtCaptcha").val("");

    //if(strUrl == undefined){
        //strUrl = '';
    //}
    //if(strMasConsultas == undefined){
        //strMasConsultas = '';
    //}
    //if(strUrl == 'Invitado' && strMasConsultas == '1'){
        //$("#CaptchaContinue").click();            
    //}else{            
        //$('#myModal').modal("show");
    //}    
//});

$(document).ready(function() {
    //Busqueda Tributaria > Seleccionar Tipo Busqueda
    //$('#busqCodAdministrado').hide();
    $('#busqTipoDocIdentidad').hide();
    $('#busqApellidosNombres').hide();
    $('#busqRazonSocial').hide();
    $('#ctl00_cplPrincipal_divBuscaPlaca').hide();
    $('#divCaptcha').hide();
    $("#tipoBusqueda").val("busqCodAdministrado").trigger("change");
    //Papeleta
    $('#ctl00_cplPrincipal_divBusPlaca').hide();
    $('#ctl00_cplPrincipal_busqPapeleta').hide();
    $('#ctl00_cplPrincipal_divBusLicencia').hide();
    $('#ctl00_cplPrincipal_divBusDNI').hide();
    $('#ctl00_cplPrincipal_divCaptchaPapeletas').hide();

    //Infracciones Conductor
    $('#divBusLicencia').hide();
    $('#divBusDNI').hide();
    $('#divCaptchaInfConductor').hide();

    //TributosRef
    $('#ctl00_cplPrincipal_divDataGrid > div').addClass('table-responsive');
    $('#ctl00_cplPrincipal_divDataGrid > div > table').addClass('table');

    //Arreglo Carrito
    let cphCarrito = '#ctl00_cplPrincipal_ucDatosCarrito1_';
    $(cphCarrito + 'lnkCarrito').append('Carrito <i class="fa fa-shopping-cart"></i>&nbsp;&nbsp;(' + $(cphCarrito + 'valCantidad').val() + ')&nbsp;' + $(cphCarrito + "valMonto").val() + '');

    let strUrl = '';
	strUrl = getUrlVars()["pla"];
	
    if (strUrl == undefined) {
        strUrl = '';
        //} else if (strUrl != undefined && strCodCapcha.length >= 3) {
    } else if (strUrl != undefined ) {
        //$('#ctl00_cplPrincipal_divBusPlaca').hide();
        //} else {
        $("#tipoBusquedaPapeletas option[value='busqPlaca']").prop('selected', true);
        $('#ctl00_cplPrincipal_divBusPlaca').show();
        $('#ctl00_cplPrincipal_busqPapeleta').hide();
        $('#ctl00_cplPrincipal_divBusLicencia').hide();
        $('#ctl00_cplPrincipal_divBusDNI').hide();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqPlaca");
        $('#ctl00_cplPrincipal_btnExcesoPap').hide();
    }

    strUrl = getUrlVars()["TRI"];   
    //if (strUrl == undefined) {
        //strUrl = '';
    //} else
        if (strUrl == 'V') {
        $("#tipoBusqueda option[value='divBuscaPlaca']").prop('selected', true);//revisar
        $('#ctl00_cplPrincipal_divBuscaPlaca').show();
        $('#divCaptcha').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("divBuscaPlaca");

    } else if (strUrl == 'T') {
        $("#tipoBusqueda option[value='busqTipoDocIdentidad']").prop('selected', true);
        $('#busqTipoDocIdentidad').show();
        $('#divCaptcha').show();
        $('#divCarrito').hide();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqTipoDocIdentidad");
    }

    //Arreglo Botones
    if ($(window).width() <= 1024) {
        $('#ctl00_cplPrincipal_btnBuscar').css('margin-top', '15px');
        $('#ctl00_cplPrincipal_btnVerReferen').css('margin-top', '15px');
    }

    if ($(window).width() <= 640) {
        $('#btnPrint').css('margin-top', '15px');
    }

    
});

$(window).resize(function() {
    if ($(window).width() <= 1024) {
        $('#ctl00_cplPrincipal_btnBuscar').css('margin-top', '15px');
        $('#ctl00_cplPrincipal_btnVerReferen').css('margin-top', '15px');
    }   
});


$('#busqCodAdministrado').show();
$('#tipoBusqueda').on('change', function() {
    if ($(this).val() == "Seleccionar") {
        $('#busqCodAdministrado').hide();
        $('#busqTipoDocIdentidad').hide();
        $('#busqApellidosNombres').hide();
        $('#busqRazonSocial').hide();
        $('#ctl00_cplPrincipal_divBuscaPlaca').hide();
        $('#divCaptcha').hide();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("Seleccionar");
    } else if ($(this).val() == "busqCodAdministrado") {
        $('#busqCodAdministrado').show();
        $('#busqTipoDocIdentidad').hide();
        $('#busqApellidosNombres').hide();
        $('#busqRazonSocial').hide();
        $('#ctl00_cplPrincipal_divBuscaPlaca').hide();
        $('#divCaptcha').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqCodAdministrado");
    } else if ($(this).val() == "busqTipoDocIdentidad") {
        $('#busqCodAdministrado').hide();
        $('#busqTipoDocIdentidad').show();
        $('#busqApellidosNombres').hide();
        $('#busqRazonSocial').hide();
        $('#ctl00_cplPrincipal_divBuscaPlaca').hide();
        $('#divCaptcha').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqTipoDocIdentidad");
    } else if ($(this).val() == "busqApellidosNombres") {
        $('#busqCodAdministrado').hide();
        $('#busqTipoDocIdentidad').hide();
        $('#busqApellidosNombres').show();
        $('#busqRazonSocial').hide();
        $('#ctl00_cplPrincipal_divBuscaPlaca').hide();
        $('#divCaptcha').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqApellidosNombres");
    } else if ($(this).val() == "busqRazonSocial") {
        $('#busqCodAdministrado').hide();
        $('#busqTipoDocIdentidad').hide();
        $('#busqApellidosNombres').hide();
        $('#busqRazonSocial').show();
        $('#ctl00_cplPrincipal_divBuscaPlaca').hide();
        $('#divCaptcha').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqRazonSocial");
    } else if ($(this).val() == "divBuscaPlaca") {
        $('#busqCodAdministrado').hide();
        $('#busqTipoDocIdentidad').hide();
        $('#busqApellidosNombres').hide();
        $('#busqRazonSocial').hide();
        $('#ctl00_cplPrincipal_divBuscaPlaca').show();
        $('#divCaptcha').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("divBuscaPlaca");
    }
});

$('#tipoBusquedaPapeletas').on('change', function() {
    if ($(this).val() == "Seleccionar") {
        $('#ctl00_cplPrincipal_divBusPlaca').hide();
        $('#ctl00_cplPrincipal_busqPapeleta').hide();
        $('#ctl00_cplPrincipal_divBusLicencia').hide();
        $('#ctl00_cplPrincipal_divBusDNI').hide();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').hide();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("Seleccionar");
        $('#ctl00_cplPrincipal_btnExcesoPap').show();
    } else if ($(this).val() == "busqPlaca") {
        $('#ctl00_cplPrincipal_divBusPlaca').show();
        $('#ctl00_cplPrincipal_busqPapeleta').hide();
        $('#ctl00_cplPrincipal_divBusLicencia').hide();
        $('#ctl00_cplPrincipal_divBusDNI').hide();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqPlaca");
        $('#ctl00_cplPrincipal_btnExcesoPap').hide();
    } else if ($(this).val() == "busqPapeleta") {
        $('#ctl00_cplPrincipal_hidCabecera').val("Numero de multa");
        $('#ctl00_cplPrincipal_divBusPlaca').hide();
        $('#ctl00_cplPrincipal_busqPapeleta').show();
        $('#ctl00_cplPrincipal_divBusLicencia').hide();
        $('#ctl00_cplPrincipal_divBusDNI').hide();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqPapeleta");
        $('#ctl00_cplPrincipal_btnExcesoPap').hide();
    } else if ($(this).val() == "busqLicencia") {
        $('#ctl00_cplPrincipal_divBusPlaca').hide();
        $('#ctl00_cplPrincipal_busqPapeleta').hide();
        $('#ctl00_cplPrincipal_divBusLicencia').show();
        $('#ctl00_cplPrincipal_divBusDNI').hide();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqLicencia");
        $('#ctl00_cplPrincipal_btnExcesoPap').hide();
    } else if ($(this).val() == "busqDNI")  {
        $('#ctl00_cplPrincipal_divBusPlaca').hide();
        $('#ctl00_cplPrincipal_busqPapeleta').hide();
        $('#ctl00_cplPrincipal_divBusLicencia').hide();
        $('#ctl00_cplPrincipal_divBusDNI').show();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqDNI");
        $('#ctl00_cplPrincipal_btnExcesoPap').hide();
        $('#ctl00_cplPrincipal_txtDNI').attr('placeholder', 'Ingrese DNI');
        $('#ctl00_cplPrincipal_lblSubTitulo').text("BÚSQUEDA POR DNI");      
        $('#ctl00_cplPrincipal_lblBuscar').text("DNI:");    
        $('#ctl00_cplPrincipal_txtDNI').attr('maxlength', 8);   
        $('#ctl00_cplPrincipal_txtDNI').val('');               
    } else if ($(this).val() == "busqCE")  {
        $('#ctl00_cplPrincipal_divBusPlaca').hide();
        $('#ctl00_cplPrincipal_busqPapeleta').hide();
        $('#ctl00_cplPrincipal_divBusLicencia').hide();
        $('#ctl00_cplPrincipal_divBusDNI').show();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqCE");
        $('#ctl00_cplPrincipal_btnExcesoPap').hide();
        $('#ctl00_cplPrincipal_txtDNI').attr('placeholder', 'Ingrese CE');
        $('#ctl00_cplPrincipal_lblSubTitulo').text("BÚSQUEDA POR CARNET DE EXTRANJERÍA");        
        $('#ctl00_cplPrincipal_lblBuscar').text("Carnet de extranjería:");    
        $('#ctl00_cplPrincipal_txtDNI').attr('maxlength', 9);    
        $('#ctl00_cplPrincipal_txtDNI').val('');    
    } else if ($(this).val() == "busqPTP")  {
        $('#ctl00_cplPrincipal_divBusPlaca').hide();
        $('#ctl00_cplPrincipal_busqPapeleta').hide();
        $('#ctl00_cplPrincipal_divBusLicencia').hide();
        $('#ctl00_cplPrincipal_divBusDNI').show();
        $('#ctl00_cplPrincipal_divCaptchaPapeletas').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqPTP");
        $('#ctl00_cplPrincipal_btnExcesoPap').hide();
        $('#ctl00_cplPrincipal_txtDNI').attr('placeholder', 'Ingrese PTP');        
        $('#ctl00_cplPrincipal_lblSubTitulo').text("BÚSQUEDA POR PERMISO TEMPORAL DE PERMANENCIA (PTP)");        
        $('#ctl00_cplPrincipal_lblBuscar').text("Permiso Temporal de Permanencia (PTP):");
        $('#ctl00_cplPrincipal_txtDNI').attr('maxlength', 9);    
        $('#ctl00_cplPrincipal_txtDNI').val('');    
    }
});

$('#tipoBusInfraConductor').on('change', function () {
    if ($(this).val() == "Seleccionar") {
        $('#divBusLicencia').hide();
        $('#divBusDNI').hide();
        $('#divCaptchaInfConductor').hide();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("Seleccionar");
    } else if ($(this).val() == "busqLicencia") {
        $('#divBusLicencia').show();
        $('#divBusDNI').hide();
        $('#divCaptchaInfConductor').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqLicencia");
    } else if ($(this).val() == "busqDNI") {
        $('#divBusLicencia').hide();
        $('#divBusDNI').show();
        $('#divCaptchaInfConductor').show();
        $('#ctl00_cplPrincipal_hidTipConsulta').val("busqDNI");
    }
});


function validaEmail(objeto) {
    let numInput = $("#" + objeto);
    numInput.val(numInput.val().replace(/\s/g, '')); 
    numInput.val(numInput.val().replace(/[^A-Za-z0-9@._-]/g, ''));            
    numInput.val(numInput.val().toUpperCase());
            
}
function validaSoloNumeroTexto(objeto) {
    let numInput = $("#" + objeto);
    numInput.val(numInput.val().replace(/\s/g, ''));
    numInput.val(numInput.val().replace(/[^A-Za-z0-9]/g, ''));
    numInput.val(numInput.val().toUpperCase());
 }