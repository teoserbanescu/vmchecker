package ro.pub.cs.vmchecker.client.presenter;

import ro.pub.cs.vmchecker.client.event.AssignmentSelectedEvent;
import ro.pub.cs.vmchecker.client.event.AssignmentSelectedEventHandler;
import ro.pub.cs.vmchecker.client.event.StatusChangedEvent;
import ro.pub.cs.vmchecker.client.model.Assignment;
import ro.pub.cs.vmchecker.client.model.UploadStatus;
import ro.pub.cs.vmchecker.client.service.HTTPService;
import ro.pub.cs.vmchecker.client.service.json.UploadResponseDecoder;
import ro.pub.cs.vmchecker.client.ui.ResultsWidget;
import ro.pub.cs.vmchecker.client.ui.StatementWidget;
import ro.pub.cs.vmchecker.client.ui.UploadWidget;

import com.google.gwt.core.client.GWT;
import com.google.gwt.event.dom.client.ClickEvent;
import com.google.gwt.event.dom.client.ClickHandler;
import com.google.gwt.event.dom.client.HasClickHandlers;
import com.google.gwt.event.logical.shared.HasSelectionHandlers;
import com.google.gwt.event.logical.shared.SelectionEvent;
import com.google.gwt.event.logical.shared.SelectionHandler;
import com.google.gwt.event.shared.HandlerManager;
import com.google.gwt.event.shared.HandlerRegistration;
import com.google.gwt.event.shared.GwtEvent.Type;
import com.google.gwt.user.client.ui.FormPanel;
import com.google.gwt.user.client.ui.HasText;
import com.google.gwt.user.client.ui.HasWidgets;
import com.google.gwt.user.client.ui.Hidden;
import com.google.gwt.user.client.ui.FormPanel.SubmitCompleteEvent;
import com.google.gwt.user.client.ui.FormPanel.SubmitCompleteHandler;
import com.google.gwt.user.client.ui.FormPanel.SubmitEvent;
import com.google.gwt.user.client.ui.FormPanel.SubmitHandler;

public class AssignmentBoardPresenter implements Presenter, SubmitCompleteHandler {

	public interface Widget {
		public static enum View {
			UPLOAD, STATEMENT, RESULTS
		}; 
		
		public static final String[] viewTitles = {
			"Trimitere solutii", "Enunt", "Rezultate"
		}; 
		
		public static final int defaultView = 0; /* UPLOAD */
		
		HasText getTitleLabel();
		HasText getDeadlineLabel();
		HasSelectionHandlers<Integer> getMenu();
		void setSelectedTab(int tabIndex); 
		void displayView(com.google.gwt.user.client.ui.Widget view); 
	}
	
	public interface UploadWidget {
		HasClickHandlers getSubmitButton(); 
		FormPanel getUploadForm();
		Hidden getCourseField(); 
		Hidden getAssignmentField();
	}
	
	private HandlerManager eventBus;
	private HTTPService service; 
	private AssignmentBoardPresenter.Widget widget;
	private HasWidgets container; 
	private HandlerRegistration assignmentSelectReg = null;
	private String courseId;
	private String assignmentId; 
	
	private UploadWidget uploadWidget = new ro.pub.cs.vmchecker.client.ui.UploadWidget();
	private StatementWidget statementWidget = new StatementWidget();
	private ResultsWidget resultsWidget = new ResultsWidget(); 
	
	public AssignmentBoardPresenter(HandlerManager eventBus, HTTPService service, String courseId, AssignmentBoardPresenter.Widget widget) {
		this.eventBus = eventBus;
		this.service = service; 
		this.courseId = courseId; 
		bindWidget(widget);
		listenAssignmentSelect();
		uploadWidget.getUploadForm().setAction(service.UPLOAD_URL); 
		uploadWidget.getUploadForm().addSubmitCompleteHandler(this); 
	}
	
	public void listenAssignmentSelect() {
		assignmentSelectReg = eventBus.addHandler(AssignmentSelectedEvent.TYPE, new AssignmentSelectedEventHandler() {
			public void onSelect(AssignmentSelectedEvent event) {
				GWT.log("Assignment select event captured: " + widget.hashCode() + " " + container.hashCode(), null);
				assignmentSelected(event.data); 
			}			
		}); 
	}
	
	public void assignmentSelected(Assignment data) {
		widget.getTitleLabel().setText(data.title);
		this.assignmentId = data.id; 
		widget.getDeadlineLabel().setText(data.deadline);
		setVisibleView(Widget.defaultView);
		widget.setSelectedTab(Widget.defaultView); 
		((com.google.gwt.user.client.ui.Widget)widget).setVisible(true);
	}
	
	public void setVisibleView(int viewIndex) {
		Widget.View view = Widget.View.values()[viewIndex];
		switch (view) {
		case UPLOAD: 
			widget.displayView((com.google.gwt.user.client.ui.Widget)uploadWidget); 
			break; 
		case STATEMENT: 
			widget.displayView(statementWidget); 
			break; 			
		case RESULTS:
			widget.displayView(resultsWidget); 
			break; 
		}
	}
	
	private void bindWidget(AssignmentBoardPresenter.Widget widget) {
		this.widget = widget;
		listenMenuSelection();
		listenSubmitUpload(); 
	}
	
	private void listenSubmitUpload() {
		uploadWidget.getSubmitButton().addClickHandler(new ClickHandler() {

			public void onClick(ClickEvent event) {
				uploadWidget.getCourseField().setValue(courseId); 
				uploadWidget.getAssignmentField().setValue(assignmentId);
				eventBus.fireEvent(new StatusChangedEvent(StatusChangedEvent.StatusType.ACTION, "Sending file...")); 
				uploadWidget.getUploadForm().submit(); 
			}
		}); 

	}
	
	private void listenMenuSelection() {
		widget.getMenu().addSelectionHandler(new SelectionHandler<Integer> () {
			
			public void onSelection(SelectionEvent<Integer> event) {
				setVisibleView(event.getSelectedItem().intValue()); 
			}
		}); 
	}
	
	@Override
	public void go(HasWidgets container) {
		this.container = container;  
		container.clear(); 
		((com.google.gwt.user.client.ui.Widget)widget).setVisible(false); 
		container.add((com.google.gwt.user.client.ui.Widget)widget);
	}

	@Override
	public void clearEventHandlers() {
		assignmentSelectReg.removeHandler();
	}

	@Override
	public void onSubmitComplete(SubmitCompleteEvent event) {
		UploadResponseDecoder responseDecoder = new UploadResponseDecoder(); 
		UploadStatus response = responseDecoder.decode(event.getResults());
		StatusChangedEvent statusChangeEvent = null; 
		if (response.status) {
			statusChangeEvent = new StatusChangedEvent(StatusChangedEvent.StatusType.SUCCESS, 
					"File uploaded successfully");
		} else {
			statusChangeEvent = new StatusChangedEvent(StatusChangedEvent.StatusType.ERROR,
					"Error uploading file"); 
		}
		eventBus.fireEvent(statusChangeEvent); 
	}

}
